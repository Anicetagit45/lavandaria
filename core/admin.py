from django.contrib import admin
from django.db import models
from unfold.admin import ModelAdmin, StackedInline
from django.db.models import Prefetch
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


"""
Action Django Admin — Relatório Financeiro (Caixa) — Opção B
Optimizado para performance.

FONTES DE LENTIDÃO CORRIGIDAS:
  1. Prefetch sem limite de campos → only() nos campos necessários.
  2. pagamentos_periodo materializado com select_related pesado →
     substituído por uma única query agregada no banco (values + annotate).
  3. aviso_caixa_divergente percorria a lista duas vezes em Python →
     calculado numa query SQL separada e leve.
  4. total_faturado iterava todos os pedidos em Python →
     calculado com aggregate() no banco.
  5. Resumos agregados disparavam 4 queries independentes →
     mantidos como querysets lazy (Django avalia no template apenas uma vez).
  6. xhtml2pdf (pisa) é lento para tabelas grandes → tabela de movimentos
     paginada no template (máx. 500 linhas) com aviso se truncada.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.db.models import (
    Count, DecimalField, OuterRef, Prefetch,
    Subquery, Sum, Value,
)
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from xhtml2pdf import pisa

from .models import PagamentoPedido


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

ZERO = Value(Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2))

# Limite de linhas na tabela de movimentos para não sobrecarregar o xhtml2pdf.
LIMITE_MOVIMENTOS = 500


def _coalesce_sum(field: str):
    return Coalesce(Sum(field), ZERO)


# ─────────────────────────────────────────────────────────────────────────────
# Parser de período
# ─────────────────────────────────────────────────────────────────────────────

def _parse_periodo(request):
    """
    Lê o período do filtro RangeDateTimeFilter do Unfold.

    O RangeDateTimeFilter gera 4 parâmetros na URL:
      data_pagamento_from_0 → data início  (YYYY-MM-DD)
      data_pagamento_from_1 → hora início  (HH:MM:SS)
      data_pagamento_to_0   → data fim     (YYYY-MM-DD)
      data_pagamento_to_1   → hora fim     (HH:MM:SS)

    Também suporta os parâmetros legados criado_em__gte / criado_em__lte
    para compatibilidade com filtros antigos.
    """
    invertidas = False

    # ── Parâmetros do RangeDateTimeFilter (Unfold) ────────────────────────────
    from_date = request.GET.get('data_pagamento_from_0', '').strip()
    from_time = request.GET.get('data_pagamento_from_1', '').strip() or '00:00:00'
    to_date   = request.GET.get('data_pagamento_to_0',   '').strip()
    to_time   = request.GET.get('data_pagamento_to_1',   '').strip() or '23:59:59'

    if from_date and to_date:
        try:
            start_dt = timezone.make_aware(
                datetime.strptime(f'{from_date} {from_time}', '%Y-%m-%d %H:%M:%S')
            )
            end_dt = timezone.make_aware(
                datetime.strptime(f'{to_date} {to_time}', '%Y-%m-%d %H:%M:%S')
            )
            if start_dt > end_dt:
                start_dt, end_dt = end_dt, start_dt
                invertidas = True
            return start_dt, end_dt, invertidas
        except ValueError:
            pass  # formato inesperado → cai no fallback abaixo

    # ── Fallback: parâmetros legados (criado_em__gte / lte) ──────────────────
    data_inicio = request.GET.get('criado_em__gte', '').strip()
    data_fim    = request.GET.get('criado_em__lte', '').strip()

    if data_inicio and data_fim:
        try:
            start_dt = timezone.make_aware(datetime.strptime(data_inicio, '%Y-%m-%d'))
            end_dt   = timezone.make_aware(
                datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1, seconds=-1)
            )
            if start_dt > end_dt:
                start_dt, end_dt = end_dt, start_dt
                invertidas = True
            return start_dt, end_dt, invertidas
        except ValueError:
            pass

    # ── Último fallback: últimos 30 dias ─────────────────────────────────────
    end_dt   = timezone.now()
    start_dt = end_dt - timedelta(days=30)
    return start_dt, end_dt, invertidas


# ─────────────────────────────────────────────────────────────────────────────
# Action principal
# ─────────────────────────────────────────────────────────────────────────────

def gerar_relatorio_financeiro(modeladmin, request, queryset):

    # ── 1. PERÍODO ────────────────────────────────────────────────────────────
    start_dt, end_dt, aviso_datas_invertidas = _parse_periodo(request)

    # ── 2. LAVANDARIA DO UTILIZADOR ───────────────────────────────────────────
    #
    # Determina a lavandaria do utilizador logado.
    # Todos os querysets abaixo são restritos a esta lavandaria,
    # garantindo que um gerente/caixa só vê dados da sua unidade.
    # Superuser vê tudo (lavandaria_do_user = None).
    #
    try:
        lavandaria_do_user = request.user.funcionario.lavandaria
    except Exception:
        lavandaria_do_user = None  # superuser ou sem funcionário → sem restrição

    # ── 3. PEDIDOS — prefetch só com os campos necessários ────────────────────
    pagamentos_prefetch = Prefetch(
        'pagamentos',
        queryset=(
            PagamentoPedido.objects
            .only('id', 'pedido_id', 'valor', 'pago_em', 'metodo_pagamento',
                  'criado_por_id')
            .select_related('criado_por__user')
            .order_by('pago_em', 'id')
        ),
        to_attr='todos_os_pagamentos',
    )

    # O queryset já vem filtrado pelo get_queryset do PedidoAdmin
    # (que restringe por lavandaria). Aplicamos .only() para performance.
    #
    # NOTA: total_final é @property — não pode ir em .only() nem aggregate().
    pedidos = list(
        queryset
        .select_related('cliente', 'lavandaria', 'funcionario')
        .only(
            'id', 'criado_em',
            'total', 'desconto', 'desconto_cabides',
            'cliente__id', 'cliente__nome',
            'lavandaria__id', 'lavandaria__nome', 'lavandaria__endereco',
            'funcionario__id',
        )
        .prefetch_related(pagamentos_prefetch)
        .order_by('criado_em')
    )

    # ── 4. TOTAL FATURADO — soma em Python (total_final é @property) ─────────
    total_faturado = sum(p.total_final for p in pedidos)

    # ── 5. CAIXA DO PERÍODO — restrito à lavandaria do utilizador ────────────
    #
    # Opção B: filtra por pago_em no período (inclui pedidos antigos).
    # CORRECÇÃO: restringe também à lavandaria do utilizador para que
    # um caixa da Lavandaria A não veja pagamentos da Lavandaria B.
    #
    pagamentos_periodo_qs = (
        PagamentoPedido.objects
        .filter(pago_em__gte=start_dt, pago_em__lte=end_dt)
        .select_related(
            'pedido__cliente',
            'pedido__lavandaria',
            'criado_por__user',
        )
        .only(
            'id', 'valor', 'pago_em', 'metodo_pagamento',
            'pedido_id',
            'pedido__id', 'pedido__criado_em',
            'pedido__cliente__nome',
            'pedido__lavandaria__nome',
            'criado_por__user__username',
        )
        .order_by('pago_em', 'id')
    )

    # Restringe à lavandaria do utilizador (não superuser).
    # Garante que caixa/gerente só vê movimentos da sua unidade.
    if lavandaria_do_user is not None:
        pagamentos_periodo_qs = pagamentos_periodo_qs.filter(
            pedido__lavandaria=lavandaria_do_user
        )

    # OPTIMIZAÇÃO 3: total_recebido calculado no banco com aggregate(),
    # não iterando a lista em Python.
    total_recebido = (
        pagamentos_periodo_qs
        .aggregate(t=_coalesce_sum('valor'))['t']
    )

    # OPTIMIZAÇÃO 4: aviso_caixa_divergente calculado com uma query leve
    # (EXISTS + SUM restrito) em vez de percorrer a lista duas vezes.
    ids_queryset = set(queryset.values_list('id', flat=True))
    total_recebido_so_queryset = (
        pagamentos_periodo_qs
        .filter(pedido_id__in=ids_queryset)
        .aggregate(t=_coalesce_sum('valor'))['t']
    )
    aviso_caixa_divergente = total_recebido != total_recebido_so_queryset

    # OPTIMIZAÇÃO 5: materializa os movimentos com limite para o template,
    # evitando que xhtml2pdf renderize milhares de linhas de uma vez.
    total_movimentos        = pagamentos_periodo_qs.count()
    aviso_movimentos_truncados = total_movimentos > LIMITE_MOVIMENTOS
    pagamentos_periodo      = list(pagamentos_periodo_qs[:LIMITE_MOVIMENTOS])

    # ── 5. LOOP POR PEDIDO ────────────────────────────────────────────────────
    #
    # Os pedidos já estão em memória com os pagamentos prefetchados.
    # Não há queries adicionais aqui.
    #
    saldo_total       = Decimal('0.00')
    pedidos_em_aberto = []

    for p in pedidos:
        todos = getattr(p, 'todos_os_pagamentos', [])

        pago_no_periodo = sum(
            pg.valor for pg in todos
            if start_dt <= pg.pago_em <= end_dt
        )
        total_pago_historico = sum(pg.valor for pg in todos)

        saldo_real = max(
            p.total_final - total_pago_historico,
            Decimal('0.00'),
        )

        if saldo_real > Decimal('0.01'):
            desconto_geral      = p.desconto         or Decimal('0.00')
            desconto_cabides    = p.desconto_cabides or Decimal('0.00')
            desconto_fidelidade = max(
                p.total - p.total_final - desconto_cabides - desconto_geral,
                Decimal('0.00'),
            )
            desconto_total = desconto_geral + desconto_cabides + desconto_fidelidade

            percentual_recebido = (
                float(total_pago_historico / p.total_final * 100)
                if p.total_final and p.total_final > 0
                else 0.0
            )

            pedidos_em_aberto.append({
                'pedido':               p,
                'total_final':          p.total_final,
                'pago_no_periodo':      pago_no_periodo,
                'total_pago_historico': total_pago_historico,
                'saldo':                saldo_real,
                'desconto_geral':       desconto_geral,
                'desconto_cabides':     desconto_cabides,
                'desconto_fidelidade':  desconto_fidelidade,
                'desconto_total':       desconto_total,
                'percentual_recebido':  percentual_recebido,
                'pagamentos_do_pedido': todos,
            })
            saldo_total += saldo_real

    # ── 6. RESUMOS AGREGADOS — 4 queries leves no banco ──────────────────────
    #
    # OPTIMIZAÇÃO 6: querysets lazy passados ao template — o Django ORM
    # executa cada um como uma única query GROUP BY, muito mais rápido
    # do que qualquer agregação em Python.
    #
    resumo_por_metodo = (
        pagamentos_periodo_qs
        .values('metodo_pagamento')
        .annotate(qtd=Count('id'), total=_coalesce_sum('valor'))
        .order_by('-total')
    )

    resumo_por_dia = (
        pagamentos_periodo_qs
        .values('pago_em__date')
        .annotate(qtd=Count('id'), total=_coalesce_sum('valor'))
        .order_by('pago_em__date')
    )

    resumo_por_lavandaria = (
        pagamentos_periodo_qs
        .values('pedido__lavandaria__nome')
        .annotate(qtd=Count('id'), total=_coalesce_sum('valor'))
        .order_by('-total')
    )

    resumo_por_caixa = (
        pagamentos_periodo_qs
        .values('criado_por__user__username')
        .annotate(qtd=Count('id'), total=_coalesce_sum('valor'))
        .order_by('-total')
    )

    # ── 7. LAVANDARIA DO UTILIZADOR ───────────────────────────────────────────
    # Já calculada no início (lavandaria_do_user) — reutilizamos aqui.
    lavandaria = lavandaria_do_user

    fmt = '%d/%m/%Y'
    start_date_simple = timezone.localtime(start_dt).strftime(fmt)
    end_date_simple   = timezone.localtime(end_dt).strftime(fmt)

    # ── 8. RENDER + PDF ───────────────────────────────────────────────────────
    context = {
        'lavandaria':                  lavandaria,
        'start_date':                  start_date_simple,
        'end_date':                    end_date_simple,
        # Totais
        'total_faturado':              total_faturado,
        'total_recebido':              total_recebido,
        'saldo_total':                 saldo_total,
        # Resumos
        'resumo_por_metodo':           resumo_por_metodo,
        'resumo_por_dia':              resumo_por_dia,
        'resumo_por_lavandaria':       resumo_por_lavandaria,
        'resumo_por_caixa':            resumo_por_caixa,
        # Detalhe por pedido
        'pedidos_em_aberto':           pedidos_em_aberto,
        'pedidos':                     pedidos,
        'pagamentos':                  pagamentos_periodo,
        'total_movimentos':            total_movimentos,
        # Avisos
        'aviso_datas_invertidas':      aviso_datas_invertidas,
        'aviso_caixa_divergente':      aviso_caixa_divergente,
        'aviso_movimentos_truncados':  aviso_movimentos_truncados,
        'limite_movimentos':           LIMITE_MOVIMENTOS,
    }

    html_string = render_to_string('core/relatorio_financeiro.html', context)

    buffer      = BytesIO()
    filename    = f'relatorio_financeiro_{start_date_simple}_a_{end_date_simple}.pdf'
    pisa_status = pisa.CreatePDF(html_string, dest=buffer)

    if pisa_status.err:
        messages.error(request, 'Erro ao gerar o PDF do relatório financeiro.')
        return HttpResponse('Erro ao gerar PDF', content_type='text/plain')

    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


gerar_relatorio_financeiro.short_description = 'Gerar relatório financeiro (PDF)'




@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm

    # Forms loaded from `unfold.forms`
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    actions = ["tornar_gerente", "tornar_caixa"]

    @admin.action(description="Tornar Gerente")
    def tornar_gerente(self, request, queryset):
        grupo = Group.objects.get(name="gerente")

        for user in queryset:
            user.groups.set([grupo])

            # Atualiza também o Funcionario (se existir)
            if hasattr(user, 'funcionario'):
                user.funcionario.grupo = "gerente"
                user.funcionario.save(update_fields=["grupo"])

        messages.success(request, "Usuários atualizados para GERENTE.")

    @admin.action(description="Tornar Caixa")
    def tornar_caixa(self, request, queryset):
        grupo = Group.objects.get(name="caixa")

        for user in queryset:
            user.groups.set([grupo])

            if hasattr(user, 'funcionario'):
                user.funcionario.grupo = "caixa"
                user.funcionario.save(update_fields=["grupo"])

        messages.success(request, "Usuários atualizados para CAIXA.")

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















