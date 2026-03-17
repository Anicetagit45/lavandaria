from datetime import timedelta
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Sum, Avg, Max, Min, Q
from django.utils import timezone

from core.models import Cliente, Pedido, Funcionario


@staff_member_required
def crm_pos_venda(request):
    hoje = timezone.now()
    limite_ativos = hoje - timedelta(days=30)
    limite_risco = hoje - timedelta(days=45)
    ultimos_7_dias = hoje - timedelta(days=6)

    # ======================
    # LAVANDARIA CONTEXTO (IGUAL AO ADMIN)
    # ======================
    lavandaria = None
    if not request.user.is_superuser:
        try:
            funcionario = Funcionario.objects.get(user=request.user)
            lavandaria = funcionario.lavandaria
            if not lavandaria:
                raise ValueError("O funcionário logado não está associado a nenhuma lavandaria.")
        except Funcionario.DoesNotExist:
            raise ValueError("O usuário logado não está associado a nenhum funcionário.")

    # ======================
    # FILTROS (GET)
    # ======================
    search = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "")
    atividade_filter = request.GET.get("atividade", "")
    page_number = request.GET.get("page", 1)

    # ======================
    # BASE: PEDIDOS (FILTRADO POR LAVANDARIA)
    # ======================
    pedidos_base = Pedido.objects.all()
    if lavandaria:
        pedidos_base = pedidos_base.filter(lavandaria=lavandaria)

    # ======================
    # CLIENTES QS (ANNOTATE POR LAVANDARIA)
    # ======================
    pedidos_filter_q = Q(pedidos__isnull=False)
    if lavandaria:
        pedidos_filter_q &= Q(pedidos__lavandaria=lavandaria)

    clientes_qs = Cliente.objects.annotate(
        total_pedidos=Count("pedidos", filter=pedidos_filter_q, distinct=True),
        total_gasto=Sum("pedidos__total", filter=pedidos_filter_q),
        ultima_visita=Max("pedidos__criado_em", filter=pedidos_filter_q),
        primeira_visita=Min("pedidos__criado_em", filter=pedidos_filter_q),
    )

    # ======================
    # SEARCH
    # ======================
    if search:
        clientes_qs = clientes_qs.filter(
            Q(id__icontains=search) |
            Q(nome__icontains=search)
        )

    # ======================
    # FILTRO ATIVIDADE (DB)
    # ======================
    if atividade_filter == "ativo":
        clientes_qs = clientes_qs.filter(ultima_visita__gte=limite_ativos)

    elif atividade_filter == "risco":
        clientes_qs = clientes_qs.filter(
            ultima_visita__lt=limite_ativos,
            ultima_visita__gte=limite_risco
        )

    elif atividade_filter == "inativo":
        clientes_qs = clientes_qs.filter(ultima_visita__lt=limite_risco)

    clientes_qs = clientes_qs.order_by("-total_gasto")

    # ======================
    # KPIs (POR LAVANDARIA)
    # ======================
    # total_clientes aqui faz mais sentido como “clientes com pedidos nesta lavandaria”
    total_clientes = clientes_qs.filter(total_pedidos__gt=0).count()

    clientes_ativos = Cliente.objects.filter(
        pedidos__criado_em__gte=limite_ativos,
        **({"pedidos__lavandaria": lavandaria} if lavandaria else {})
    ).distinct().count()

    clientes_inativos = max(total_clientes - clientes_ativos, 0)

    # "em risco" baseado no último pedido (não em qualquer pedido antigo)
    clientes_em_risco = clientes_qs.filter(
        ultima_visita__lt=limite_ativos,
        ultima_visita__gte=limite_risco
    ).count()

    clientes_recorrentes = clientes_qs.filter(total_pedidos__gte=2).count()

    ticket_medio = pedidos_base.aggregate(v=Avg("total"))["v"] or 0

    ltv_medio = clientes_qs.aggregate(m=Avg("total_gasto"))["m"] or 0

    pedidos_nao_pagos = pedidos_base.filter(pago=False).count()

    clientes_vip = clientes_qs.filter(total_gasto__gte=10000).count()

    kpis = [
        {"title": "Clientes (base)", "metric": total_clientes},
        {"title": "Clientes Ativos", "metric": clientes_ativos},
        {"title": "Clientes Inativos", "metric": clientes_inativos},
        {"title": "Clientes em Risco", "metric": clientes_em_risco},
        {"title": "Clientes Recorrentes", "metric": clientes_recorrentes},
        {"title": "Ticket Médio", "metric": f"{ticket_medio:.2f} MT"},
        {"title": "LTV Médio", "metric": f"{ltv_medio:.2f} MT"},
        {"title": "Clientes VIP", "metric": clientes_vip},
        {"title": "Pedidos Não Pagos", "metric": pedidos_nao_pagos},
    ]

    # ======================
    # GRÁFICO – PEDIDOS (7 DIAS)
    # ======================
    pedidos_qs = (
        pedidos_base
        .filter(criado_em__date__gte=ultimos_7_dias.date())
        .extra(select={"dia": "DATE(criado_em)"})
        .values("dia")
        .annotate(total=Count("id"))
        .order_by("dia")
    )

    labels, pedidos_data = [], []
    for i in range(7):
        dia = (ultimos_7_dias + timedelta(days=i)).date()
        labels.append(dia.strftime("%d/%m"))
        valor = next((p["total"] for p in pedidos_qs if p["dia"] == dia), 0)
        pedidos_data.append(valor)

    pedidosChartData = {"labels": labels, "datasets": [{"label": "Pedidos", "data": pedidos_data}]}

    # ======================
    # GRÁFICO – VENDAS (7 DIAS)
    # ======================
    vendas_qs = (
        pedidos_base
        .filter(criado_em__date__gte=ultimos_7_dias.date())
        .extra(select={"dia": "DATE(criado_em)"})
        .values("dia")
        .annotate(total=Sum("total"))
        .order_by("dia")
    )

    vendas_data = []
    for i in range(7):
        dia = (ultimos_7_dias + timedelta(days=i)).date()
        valor = next((v["total"] for v in vendas_qs if v["dia"] == dia), 0)
        vendas_data.append(float(valor or 0))

    vendasChartData = {"labels": labels, "datasets": [{"label": "Vendas", "data": vendas_data}]}

    # ======================
    # PAGINAÇÃO
    # ======================
    paginator = Paginator(clientes_qs, 10)
    page_obj = paginator.get_page(page_number)

    # ======================
    # TABELA
    # ======================
    tabela = []
    for c in page_obj:
        dias_sem_visita = (hoje.date() - c.ultima_visita.date()).days if c.ultima_visita else None

        if dias_sem_visita is None or dias_sem_visita > 45:
            atividade = "inativo"
        elif dias_sem_visita > 30:
            atividade = "risco"
        else:
            atividade = "ativo"

        if c.total_gasto and c.total_gasto >= 10000:
            status = "VIP"
        elif (c.total_pedidos or 0) >= 5:
            status = "Regular"
        elif (c.total_pedidos or 0) > 0:
            status = "Ocasional"
        else:
            status = "Inativo"

        if status_filter and status != status_filter:
            continue

        tabela.append({
            "id": c.id,
            "cliente": c.nome,
            "pedidos": c.total_pedidos or 0,
            "ultima_visita": f"{dias_sem_visita} dias" if dias_sem_visita is not None else "Nunca",
            "total": f"{(c.total_gasto or 0):.2f} MT",
            "status": status,
            "atividade": atividade,
        })

    context = {
        "title": "CRM Pós-Venda",
        "kpis": kpis,
        "pedidosChartData": pedidosChartData,
        "vendasChartData": vendasChartData,
        "table": {
            "headers": ["ID", "Cliente", "Pedidos", "Última Visita", "Total Gasto", "Status"],
            "rows": tabela,
        },
        "page_obj": page_obj,
        "filters": {"q": search, "status": status_filter, "atividade": atividade_filter},
        "lavandaria": lavandaria,  # opcional p/ mostrar no topo do dashboard
    }

    return render(request, "crm/crm_dashboard.html", context)
