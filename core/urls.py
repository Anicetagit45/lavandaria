from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('imprimir-recibo-imagem/<int:pedido_id>/', views.imprimir_recibo_imagem, name='imprimir_recibo_imagem'),
    path('meu-pedido/', views.meu_pedido, name='order-track'),
    path('meu-pedido/<int:pedido_id>', views.meu_pedido_details, name='order-details'),
]
