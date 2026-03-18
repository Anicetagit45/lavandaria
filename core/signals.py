from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .models import Pedido, Cliente, MovimentacaoPontos


@receiver(post_save, sender=Pedido)
def processar_fidelidade(sender, instance, created, **kwargs):
    """
    PROCESSADOR DE FIDELIDADE - SÓ EXECUTA NA CRIAÇÃO DO PEDIDO
    """
    # ⚠️ CRÍTICO: Só processa na criação do pedido, nunca em updates
    if not created:
        return

    # Prevenir processamento duplicado
    if hasattr(instance, '_fidelidade_processada'):
        return

    def processar():
        # Recarregar o pedido do banco para ter o total correto (com itens)
        pedido_atualizado = Pedido.objects.get(pk=instance.pk)

        if pedido_atualizado.total <= 0:
            return

        with transaction.atomic():
            # Bloquear cliente para evitar race conditions
            cliente = Cliente.objects.select_for_update().get(pk=pedido_atualizado.cliente.pk)

            # ===========================================
            # PASSO 1: CALCULAR PONTOS A GANHAR
            # ===========================================
            valor_gasto = pedido_atualizado.total
            pontos_ganhos = int(valor_gasto * 10)

            # ===========================================
            # PASSO 2: GANHAR PONTOS
            # ===========================================
            if not MovimentacaoPontos.objects.filter(
                    pedido=pedido_atualizado,
                    tipo="ganho"
            ).exists():
                # Adicionar pontos
                cliente.pontos += pontos_ganhos

                # Registrar ganho de pontos
                MovimentacaoPontos.objects.create(
                    cliente=cliente,
                    pedido=pedido_atualizado,
                    tipo="ganho",
                    pontos=pontos_ganhos,
                    criado_por=pedido_atualizado.funcionario
                )

            # ===========================================
            # PASSO 3: VERIFICAR DESCONTO
            # ===========================================
            DESCONTO = Decimal("250.00")
            LIMITE = 50000
            desconto_aplicado = Decimal("0.00")

            if not MovimentacaoPontos.objects.filter(
                    pedido=pedido_atualizado,
                    tipo="uso"
            ).exists() and cliente.pontos >= LIMITE:
                # Consumir pontos
                cliente.pontos -= LIMITE
                desconto_aplicado = DESCONTO

                # Registrar uso de pontos
                MovimentacaoPontos.objects.create(
                    cliente=cliente,
                    pedido=pedido_atualizado,
                    tipo="uso",
                    pontos=-LIMITE,
                    criado_por=pedido_atualizado.funcionario
                )

            # ===========================================
            # PASSO 4: ATUALIZAR PEDIDO COM DESCONTO (SE HOUVER)
            # ===========================================
            if desconto_aplicado > 0:
                # Usar update() para não disparar signals novamente
                Pedido.objects.filter(pk=pedido_atualizado.pk).update(
                    desconto=desconto_aplicado
                )

            # ===========================================
            # PASSO 5: ATUALIZAR CLIENTE
            # ===========================================
            cliente.total_gasto_acumulado += Decimal(valor_gasto)
            cliente.save(update_fields=["pontos", "total_gasto_acumulado"])

            # Marcar como processado
            instance._fidelidade_processada = True

    # Executar após o commit da transação
    transaction.on_commit(processar)
