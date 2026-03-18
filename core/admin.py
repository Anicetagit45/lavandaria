from django.contrib import admin
from django.db import models
from unfold.admin import ModelAdmin, StackedInline
# REMOVA ou modifique esta linha:
# from django import forms

# ADICIONE esta linha:
from django import forms as django_forms
from .models import Lavandaria, ItemServico, Servico, Cliente, Pedido, ItemPedido, Funcionario, Recibo, PagamentoPedido
from django.utils.html import format_html
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from django.contrib.auth.models import Group, User
from django.contrib import messages
import requests
import json
from django.urls import reverse
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm
from unfold.contrib.filters.admin import RangeDateTimeFilter
from django.template.loader import render_to_string
from xhtml2pdf import pisa
from io import BytesIO
from django.http import HttpResponse
from datetime import datetime
from django.utils import timezone
from django.contrib import admin
from django.contrib.admin import RelatedOnlyFieldListFilter, forms
from unfold.admin import ModelAdmin, StackedInline

from decimal import Decimal
from django.contrib import admin, messages
from django.db.models import Sum, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.urls import path, reverse
from django.shortcuts import redirect
from django.utils.html import format_html

from unfold.admin import ModelAdmin
from .models import Pedido, PagamentoPedido, Funcionario

admin.site.unregister(Group)
admin.site.unregister(User)


def gerar_relatorio_pdf(modeladmin, request, queryset):
    # 🔥 Otimiza queryset para evitar N+1 queries
    queryset = queryset.prefetch_related('itens')

    # 🔥 Processa os totais por pedido
    for pedido in queryset:
        itens = pedido.itens.all()
        pedido.total_quantidade = sum(item.quantidade for item in itens)
        pedido.total_valor = sum(item.preco_total for item in itens)

    # 🔥 Totais gerais
    total_quantidade = sum(pedido.total_quantidade for pedido in queryset)
    total_valor = sum(pedido.total_valor for pedido in queryset)

    # 🔥 Datas do relatório
    if queryset.exists():
        start_date = timezone.localtime(queryset.first().criado_em).strftime('%d/%m/%Y')
        end_date = timezone.localtime(queryset.last().criado_em).strftime('%d/%m/%Y')
    else:
        start_date = end_date = datetime.today().strftime('%d/%m/%Y')

    # 🔥 Renderiza HTML
    html_string = render_to_string('core/relatorio_vendas.html', {
        'pedidos': queryset,
        'total_quantidade': total_quantidade,
        'total_valor': total_valor,
        'start_date': start_date,
        'end_date': end_date
    })

    # 🔥 Cria PDF
    buffer = BytesIO()
    filename = f"relatorio_vendas_{start_date}_a_{end_date}.pdf"
    pisa_status = pisa.CreatePDF(html_string, dest=buffer)

    # 🔥 Verifica erros
    if pisa_status.err:
        return HttpResponse("Erro ao gerar PDF", content_type="text/plain")

    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


from django.db.models import Sum, Count
from django.db.models.functions import Coalesce

DECIMAL_0 = Value(Decimal("0.00"), output_field=DecimalField(max_digits=12, decimal_places=2))


def gerar_relatorio_financeiro(modeladmin, request, queryset):
    """
    RELATÓRIO FINANCEIRO (Caixa) - Corrige datas invertidas e adiciona aviso
    """

    # Filtro de datas do admin
    data_inicio = request.GET.get('criado_em__gte')
    data_fim = request.GET.get('criado_em__lte')
    aviso_datas_invertidas = False

    if data_inicio and data_fim:
        from datetime import datetime, timedelta
        start_dt = timezone.make_aware(datetime.strptime(data_inicio, '%Y-%m-%d'))
        end_dt = timezone.make_aware(datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1, seconds=-1))

        # Corrige inversão
        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt
            aviso_datas_invertidas = True
    else:
        if queryset.exists():
            # 🔥 CORREÇÃO: Usar datas do período selecionado
            # Em vez de usar as datas dos pedidos, usar período padrão
            end_dt = timezone.now()
            start_dt = end_dt - timezone.timedelta(days=30)
        else:
            now = timezone.now()
            start_dt = now - timezone.timedelta(days=1)
            end_dt = now

    # Prefetch para pagamentos
    pagamentos_prefetch = Prefetch(
        'pagamentos',
        queryset=PagamentoPedido.objects.select_related('criado_por', 'pedido'),
        to_attr='pagamentos_prefetched'
    )

    pedidos = (
        queryset
        .select_related('cliente', 'lavandaria', 'funcionario')
        .prefetch_related(pagamentos_prefetch)
        .order_by('criado_em')
    )

    # 🔥 CORREÇÃO: Pagamentos no período filtrado (USANDO pago_em)
    pagamentos_periodo = (
        PagamentoPedido.objects
        .filter(pago_em__gte=start_dt, pago_em__lte=end_dt)  # Mudado de pedido__in para filtrar direto
        .select_related("pedido", "criado_por")
        .order_by("pago_em", "id")
    )

    # Se veio um queryset de pedidos, filtrar apenas pagamentos desses pedidos
    if queryset.exists():
        pagamentos_periodo = pagamentos_periodo.filter(pedido__in=queryset)

    # Totais
    total_faturado = sum([p.total_final for p in pedidos])
    total_recebido = pagamentos_periodo.aggregate(t=Coalesce(Sum("valor"), DECIMAL_0))["t"]
    saldo_total = Decimal("0.00")
    pedidos_em_aberto = []

    for p in pedidos:
        total_pago_historico = sum([pg.valor for pg in getattr(p, 'pagamentos_prefetched', [])]) or DECIMAL_0
        pago_no_periodo = sum([pg.valor for pg in getattr(p, 'pagamentos_prefetched', []) if start_dt <= pg.pago_em <= end_dt]) or DECIMAL_0

        saldo_real = max(p.total_final - total_pago_historico, DECIMAL_0)
        if saldo_real > 0.01:
            desconto_fidelidade = max(p.total - p.total_final - (p.desconto_cabides or 0) - (p.desconto or 0), DECIMAL_0)
            desconto_total = (p.desconto or 0) + (p.desconto_cabides or 0) + desconto_fidelidade

            pedidos_em_aberto.append({
                "pedido": p,
                "total_final": p.total_final,
                "total_pago_historico": total_pago_historico,
                "pago_no_periodo": pago_no_periodo,
                "saldo": saldo_real,
                "desconto_geral": p.desconto,
                "desconto_cabides": p.desconto_cabides,
                "desconto_fidelidade": desconto_fidelidade,
                "desconto_total": desconto_total,
                "percentual_recebido": float(total_pago_historico / p.total_final * 100) if p.total_final else 0
            })
            saldo_total += saldo_real

    # Resumos agregados
    resumo_por_metodo = pagamentos_periodo.values("metodo_pagamento")\
        .annotate(qtd=Count("id"), total=Coalesce(Sum("valor"), DECIMAL_0))\
        .order_by("-total")

    resumo_por_dia = pagamentos_periodo.values("pago_em__date")\
        .annotate(qtd=Count("id"), total=Coalesce(Sum("valor"), DECIMAL_0))\
        .order_by("pago_em__date")

    resumo_por_lavandaria = pagamentos_periodo.values("pedido__lavandaria__nome")\
        .annotate(qtd=Count("id"), total=Coalesce(Sum("valor"), DECIMAL_0))\
        .order_by("-total")

    resumo_por_caixa = pagamentos_periodo.values("criado_por__user__username")\
        .annotate(qtd=Count("id"), total=Coalesce(Sum("valor"), DECIMAL_0))\
        .order_by("-total")

    try:
        lavandaria = request.user.funcionario.lavandaria
    except Exception:
        lavandaria = None

    start_date_simple = timezone.localtime(start_dt).strftime("%d/%m/%Y")
    end_date_simple = timezone.localtime(end_dt).strftime("%d/%m/%Y")

    html_string = render_to_string("core/relatorio_financeiro.html", {
        "lavandaria": lavandaria,
        "start_date": start_date_simple,
        "end_date": end_date_simple,
        "total_faturado": total_faturado,
        "total_recebido": total_recebido,
        "saldo_total": saldo_total,
        "resumo_por_metodo": resumo_por_metodo,
        "resumo_por_dia": resumo_por_dia,
        "resumo_por_lavandaria": resumo_por_lavandaria,
        "resumo_por_caixa": resumo_por_caixa,
        "pedidos_em_aberto": pedidos_em_aberto,
        "pedidos": pedidos,
        "pagamentos": pagamentos_periodo,
        "aviso_datas_invertidas": aviso_datas_invertidas,
    })

    buffer = BytesIO()
    filename = f"relatorio_financeiro_{start_date_simple}_a_{end_date_simple}.pdf"
    pisa_status = pisa.CreatePDF(html_string, dest=buffer)

    if pisa_status.err:
        return HttpResponse("Erro ao gerar PDF", content_type="text/plain")

    buffer.seek(0)
    response = HttpResponse(buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    # Forms loaded from `unfold.forms`
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    pass


class ItemPedidoInline(StackedInline):
    model = ItemPedido
    extra = 0  # Mostra 1 linha vazia para adicionar item
    fields = [
        ('item_de_servico',),
        ('descricao', 'quantidade', 'preco_total'),
    ]
    autocomplete_fields = ('item_de_servico',)
    readonly_fields = ('preco_total',)


# admin.py - Classe PagamentoPedidoInline corrigida

# admin.py - Versão alternativa mais simples




# Configuração do modelo Lavandaria no Admin
@admin.register(Lavandaria)
class LavandariaAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    list_display = ('nome', 'endereco', 'telefone', 'criado_em')
    search_fields = ('nome', 'telefone')
    list_filter = ('criado_em',)
    fieldsets = (
        ('Informações Básicas', {'fields': ('nome', 'endereco', 'telefone')}),
        ('Datas', {'fields': ('criado_em',)}),
    )
    readonly_fields = ('criado_em',)


# Configuração do modelo Cliente no Admin
@admin.register(Cliente)
class ClienteAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    list_display = ('id', 'nome', 'telefone', 'endereco', 'pontos')
    search_fields = ('nome', 'telefone')


# Configuração do modelo Funcionario no Admin
@admin.register(Funcionario)
class FuncionarioAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    list_display = ('user', 'lavandaria', 'grupo', 'telefone')
    search_fields = ('user__username', 'telefone', 'lavandaria__nome')
    list_filter = ('grupo',)


# Configuração do modelo ItemServico no Admin
@admin.register(ItemServico)
class ItemServicoAdmin(ModelAdmin, ImportExportModelAdmin):
    list_display = ('nome', 'preco_base', 'disponivel')
    search_fields = ('nome',)
    list_filter = ('disponivel',)
    import_form_class = ImportForm
    export_form_class = ExportForm

class PagamentoPedidoInline(StackedInline):
    model = PagamentoPedido
    extra = 0
    fields = (
        ("valor", "metodo_pagamento"),
        ("pago_em", "criado_por"),
    )
    readonly_fields = ("pago_em", "criado_por")
    # SEM método save_model - deixamos o admin principal gerenciar


# Configuração do modelo Servico no Admin
@admin.register(Servico)
class ServicoAdmin(ModelAdmin):
    list_display = ('nome', 'lavandaria', 'ativo')
    search_fields = ('nome', 'lavandaria__nome')
    list_filter = ('ativo', 'lavandaria')
    fieldsets = (
        ('Informações do Serviço', {'fields': ('nome', 'descricao', 'ativo')}),
        ('Lavandaria', {'fields': ('lavandaria',)}),
    )


API_URL = 'https://api.mozesms.com/v2/sms/bulk'
BEARER_TOKEN = 'Bearer 2374:zKNUpX-J4dao9-VEi60O-UeNqdN'
SENDER_ID = "POWERWASH"


def enviar_sms_mozesms(numero, mensagem):
    """
    Envia um SMS usando a API Mozesms (nova versão).
    """
    payload = {
        'sender_id': SENDER_ID,
        'messages': [
            {
                'phone': numero,
                'message': mensagem
            }
        ]
    }

    headers = {
        'Content-Type': 'application/json',
        'Authorization': BEARER_TOKEN
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers)

        if response.status_code == 200:
            try:
                json_resposta = response.json()

                if json_resposta.get('success'):
                    print("SMS enviado com sucesso!", json_resposta)
                    return True
                else:
                    print("Erro ao enviar SMS:", json_resposta)
                    return False

            except Exception as e:
                print(f"Erro ao processar a resposta JSON: {e}")
                return False
        else:
            print(f"Erro na requisição: {response.status_code} - {response.text}")
            return False

    except requests.RequestException as e:
        print(f"Erro ao enviar SMS: {e}")
        return False


# ===== FORM PERSONALIZADO PARA PEDIDO =====
class PedidoAdminForm(django_forms.ModelForm):
    """
    Form personalizado para o Pedido no Admin
    Agora usa o campo cabides_trazidos (desconto automático)
    """
    class Meta:
        model = Pedido
        fields = '__all__'
        help_texts = {
            'cabides_trazidos': 'Cada 20 cabides = 140 Mts de desconto (20=140, 40=280, 60=420)'
        }


@admin.register(Pedido)
class PedidoAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm

    form = PedidoAdminForm

    list_display = (
        "id", "cliente", "criado_em",
        "status", "status_pagamento",
        "total", "desconto", "desconto_cabides",
        "total_final", "total_pago", "saldo_admin",
        "botao_imprimir"
    )
    search_fields = ("cliente__nome", "cliente__telefone", "id", "itens__item_de_servico__nome",
                     "itens__descricao",)
    list_display_links = ("cliente", "id")

    list_filter = (
        ("funcionario", RelatedOnlyFieldListFilter),
        "status",
        "status_pagamento",
        ("data_pagamento", RangeDateTimeFilter),
        ("criado_em", RangeDateTimeFilter),
    )
    list_filter_submit = True

    fieldsets = (
        ("Detalhes do Pedido", {"fields": ("cliente", "lavandaria", "funcionario", "status")}),
        ("Totais e Datas", {"fields": ("total", "desconto", "criado_em")}),
        ("Pagamento", {"fields": ("status_pagamento", "pago", "total_pago")}),
        ("Desconto Cabides", {
            "fields": ("cabides_trazidos", "desconto_cabides"),
            "description": "Cada 20 cabides = 140 Mts de desconto (calculado automaticamente)"
        }),
    )

    readonly_fields = (
        "criado_em", "funcionario", "lavandaria",
        "total_pago", "status_pagamento", "pago",
        "total", "desconto", "desconto_cabides",
    )

    autocomplete_fields = ("cliente",)
    inlines = [ItemPedidoInline, PagamentoPedidoInline]

    # ===== MÉTODOS PRINCIPAIS =====
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        try:
            funcionario = Funcionario.objects.get(user=request.user)
            if funcionario.lavandaria:
                return qs.filter(lavandaria=funcionario.lavandaria)
            return qs.none()
        except Funcionario.DoesNotExist:
            return qs.none()

    def save_model(self, request, obj, form, change):
        """Só atribui funcionário/lavandaria em novos objetos"""
        if not change:
            try:
                funcionario = Funcionario.objects.get(user=request.user)
                obj.funcionario = funcionario
                if funcionario.lavandaria:
                    obj.lavandaria = funcionario.lavandaria
            except Funcionario.DoesNotExist:
                pass
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        """
        Salva objetos relacionados na ordem correta:
        1. Primeiro todos os objetos (itens e pagamentos)
        2. Depois atualiza o total do pedido
        3. Por fim, recalcula status de pagamento
        """
        obj = form.instance

        # PASSO 1: Processar TODOS os formsets
        for formset in formsets:
            instances = formset.save(commit=False)

            # Deletar objetos marcados
            for obj_to_delete in formset.deleted_objects:
                obj_to_delete.delete()

            # Salvar novas instâncias
            for instance in instances:
                if isinstance(instance, PagamentoPedido) and not instance.criado_por:
                    try:
                        instance.criado_por = Funcionario.objects.get(user=request.user)
                    except (Funcionario.DoesNotExist, AttributeError):
                        pass
                instance.save()

            formset.save_m2m()

        # PASSO 2: Atualizar total baseado nos itens
        if obj.pk:
            obj.atualizar_total()

        # PASSO 3: Recalcular status de pagamento
        if obj.pk:
            obj.recalcular_pagamentos()

    def save_formset(self, request, form, formset, change):
        """
        Apenas salva os objetos - o save_related cuidará dos recálculos
        """
        instances = formset.save(commit=False)

        for instance in instances:
            if isinstance(instance, PagamentoPedido) and not instance.criado_por:
                try:
                    instance.criado_por = Funcionario.objects.get(user=request.user)
                except Funcionario.DoesNotExist:
                    instance.criado_por = None
            instance.save()

        for obj_to_delete in formset.deleted_objects:
            obj_to_delete.delete()

        formset.save_m2m()

    # ===== MÉTODOS AUXILIARES =====
    def saldo_admin(self, obj):
        return obj.saldo

    saldo_admin.short_description = "Saldo"

    def botao_imprimir(self, obj):
        url = reverse("core:imprimir_recibo_imagem", args=[obj.id])
        return format_html(f'<a class="button" href="{url}" target="_blank">Imprimir</a>')

    botao_imprimir.short_description = "Imprimir Recibo"

    # ===== RESTRIÇÕES DE STATUS =====
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.pk:
            self._restrict_status_choices(form, obj)
        return form

    def _restrict_status_choices(self, form, obj):
        if "status" in form.base_fields:
            current_status = obj.status
            status_flow = {
                "pendente": ["pendente", "completo"],
                "completo": ["completo", "pronto"],
                "pronto": ["pronto", "entregue"],
                "entregue": ["entregue"],
            }
            allowed_statuses = status_flow.get(current_status, [current_status])
            form.base_fields["status"].choices = [
                c for c in form.base_fields["status"].choices
                if c[0] in allowed_statuses
            ]
            if len(allowed_statuses) == 1:
                form.base_fields["status"].disabled = True

    # ===== ACTIONS =====
    actions = [
        "marcar_como_completo",
        "marcar_como_pronto",
        "marcar_como_entregue",
        "enviar_sms_pedido_pronto",
        gerar_relatorio_pdf,
        gerar_relatorio_financeiro,
    ]

    def marcar_como_completo(self, request, queryset):
        pedidos_processados = 0
        for pedido in queryset:
            if pedido.status == "pendente":
                pedido.status = "completo"
                pedido.save(update_fields=["status"])
                pedidos_processados += 1
            else:
                messages.warning(request,
                                 f"Pedido {pedido.id} não pode ser marcado como completo. Status atual: {pedido.status}")
        if pedidos_processados:
            messages.success(request, f"{pedidos_processados} pedido(s) marcado(s) como completo.")

    marcar_como_completo.short_description = "Marcar como Completo (apenas pendentes)"

    def marcar_como_pronto(self, request, queryset):
        pedidos_processados = 0
        for pedido in queryset:
            if pedido.status == "completo":
                pedido.status = "pronto"
                pedido.save(update_fields=["status"])
                pedidos_processados += 1
            else:
                messages.warning(request,
                                 f"Pedido {pedido.id} não pode ser marcado como pronto. Status atual: {pedido.status}")
        if pedidos_processados:
            messages.success(request, f"{pedidos_processados} pedido(s) marcado(s) como pronto.")

    marcar_como_pronto.short_description = "Marcar como Pronto (apenas completo)"

    def marcar_como_entregue(self, request, queryset):
        pedidos_processados = 0
        for pedido in queryset:
            if pedido.status == "pronto":
                pedido.status = "entregue"
                pedido.save(update_fields=["status"])
                pedidos_processados += 1
            else:
                messages.warning(request,
                                 f"Pedido {pedido.id} não pode ser marcado como entregue. Status atual: {pedido.status}")
        if pedidos_processados:
            messages.success(request, f"{pedidos_processados} pedido(s) marcado(s) como entregue.")

    marcar_como_entregue.short_description = "Marcar como Entregue (apenas prontos)"

    def enviar_sms_pedido_pronto(self, request, queryset):
        pedidos_notificados = 0
        for pedido in queryset:
            if pedido.status == 'pronto' and hasattr(pedido.cliente, 'telefone'):
                link_pedido = f"https://lavandaria-production.up.railway.app/meu-pedido/{pedido.id}"
                mensagem = f"Ola {pedido.cliente.nome}, o seu artigo #{pedido.id} esta pronto, para o levantamento. Para mais info. Clique aqui {link_pedido}"
                resposta = enviar_sms_mozesms(pedido.cliente.telefone, mensagem)
                if resposta:
                    pedidos_notificados += 1
        if pedidos_notificados:
            messages.success(request, f"Mensagem enviada com sucesso para {pedidos_notificados} clientes.")
        else:
            messages.warning(request,
                             "ERRO. Verifique se os pedidos estão 'prontos' e se os clientes têm número de telefone.")

    enviar_sms_pedido_pronto.short_description = "Enviar mensagem de pedido pronto"





# Configuração do modelo ItemPedido no Admin
@admin.register(ItemPedido)
class ItemPedidoAdmin(ModelAdmin):
    list_display = ('pedido', 'item_de_servico', 'quantidade', 'preco_total')
    search_fields = ('pedido__id', 'item_de_servico__nome')
    list_filter = ('servico',)
    readonly_fields = ('preco_total',)
    autocomplete_fields = ('item_de_servico',)


@admin.register(Recibo)
class ReciboAdmin(ModelAdmin):
    list_display = ('id', 'pedido', 'total_pago', 'emitido_em', 'metodo_pagamento', 'criado_por')
    autocomplete_fields = ('pedido',)
    readonly_fields = ('emitido_em', 'criado_por')

    def save_model(self, request, obj, form, change):
        try:
            # Obtém o funcionário associado ao usuário logado
            criado_por = Funcionario.objects.get(user=request.user)
            obj.funcionario = criado_por

            # Verifica se o funcionário tem uma lavandaria associada
            if criado_por.lavandaria:
                obj.lavandaria = criado_por.lavandaria
            else:
                raise ValueError("O funcionário logado não está associado a nenhuma lavandaria.")
        except Funcionario.DoesNotExist:
            raise ValueError("O usuário logado não está associado a nenhum funcionário.")

        super().save_model(request, obj, form, change)


def _total_pago(pedido: Pedido) -> Decimal:
    return (
            PagamentoPedido.objects.filter(pedido=pedido)
            .aggregate(
                total=Coalesce(
                    Sum("valor"),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )["total"]
            or Decimal("0.00")
    )


def _saldo(pedido: Pedido) -> Decimal:
    total = pedido.total or Decimal("0.00")
    pago = _total_pago(pedido)
    s = total - pago
    return s if s > 0 else Decimal("0.00")



@admin.register(PagamentoPedido)
class PagamentoPedidoAdmin(ModelAdmin):
    list_display = ("id", "pedido", "valor", "metodo_pagamento", "pago_em", "criado_por")
    list_filter = (
        "metodo_pagamento",
        ("pago_em", RangeDateTimeFilter),
    )
    search_fields = ("pedido__id", "pedido__cliente__nome", "pedido__cliente__telefone")
    autocomplete_fields = ("pedido",)
    readonly_fields = ("criado_por",)

    actions = ["receber_saldo_pedidos_selecionados"]

    def save_model(self, request, obj, form, change):
        if not obj.criado_por_id:
            obj.criado_por = Funcionario.objects.get(user=request.user)
        if not obj.pago_em:
            obj.pago_em = timezone.now()

        super().save_model(request, obj, form, change)

        # Recalcula o pedido
        if hasattr(obj.pedido, "recalcular_pagamentos"):
            obj.pedido.recalcular_pagamentos()

    # ====== BOTÃO "RECEBER SALDO" NO PEDIDO ======
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "receber-saldo/<int:pedido_id>/",
                self.admin_site.admin_view(self.receber_saldo_view),
                name="core_receber_saldo",
            ),
        ]
        return custom + urls

    def receber_saldo_view(self, request, pedido_id):
        pedido = Pedido.objects.get(pk=pedido_id)
        saldo = _saldo(pedido)

        if saldo <= 0:
            messages.warning(request, f"Pedido {pedido.id} já está quitado.")
            return redirect(reverse("admin:core_pedido_changelist"))

        # cria pagamento “automaticamente” (método default: numerario)
        PagamentoPedido.objects.create(
            pedido=pedido,
            valor=saldo,
            metodo_pagamento="numerario",  # podes trocar para um default teu
            criado_por=Funcionario.objects.get(user=request.user),
            pago_em=timezone.now(),
        )

        if hasattr(pedido, "recalcular_pagamentos"):
            pedido.recalcular_pagamentos()

        messages.success(request, f"Recebido saldo do Pedido {pedido.id}: {saldo:.2f} MZN")
        return redirect(reverse("admin:core_pedido_change", args=[pedido.id]))

    # ====== ACTION: RECEBER SALDO DOS PEDIDOS SELECIONADOS ======
    @admin.action(description="Receber saldo dos pedidos selecionados (gera pagamento numerário)")
    def receber_saldo_pedidos_selecionados(self, request, queryset):
        """
        Esta action é chamada na lista de PagamentoPedido, mas vamos ignorar
        os pagamentos e usar os pedidos associados.
        """
        pedidos = Pedido.objects.filter(id__in=queryset.values_list("pedido_id", flat=True)).distinct()
        funcionario = Funcionario.objects.get(user=request.user)

        feitos = 0
        for pedido in pedidos:
            saldo = _saldo(pedido)
            if saldo <= 0:
                continue
            PagamentoPedido.objects.create(
                pedido=pedido,
                valor=saldo,
                metodo_pagamento="numerario",
                criado_por=funcionario,
                pago_em=timezone.now(),
            )
            if hasattr(pedido, "recalcular_pagamentos"):
                pedido.recalcular_pagamentos()
            feitos += 1

        if feitos:
            messages.success(request, f"{feitos} pedido(s) quitado(s) com pagamento do saldo.")
        else:
            messages.warning(request, "Nenhum pedido com saldo pendente.")















