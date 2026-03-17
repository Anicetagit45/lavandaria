from django import template

register = template.Library()


@register.filter(name='ljust')
def ljust(value, length):
    return str(value).ljust(length)


@register.filter(name='sum_values')
def sum_values(queryset, field_name):
    return sum(getattr(item, field_name) for item in queryset)


@register.filter(name='sum_pagos')
def sum_pagos(queryset, campo):
    """Soma os valores apenas dos pedidos pagos"""
    return sum(getattr(item, campo) for item in queryset if item.pago)


@register.filter(name='sum_nao_pagos')
def sum_nao_pagos(queryset, campo):
    """Soma os valores apenas dos pedidos não pagos"""
    return sum(getattr(item, campo) for item in queryset if not item.pago)


@register.filter
def currency_mzn(value):
    try:
        value = float(value)
        return f"{value:,.2f} MZN".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "0,00 MZN"
