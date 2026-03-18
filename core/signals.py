# Em core/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from decimal import Decimal
from .models import Pedido, Cliente, MovimentacaoPontos


@receiver(post_save, sender=Pedido)
def processar_fidelidade(sender, instance, created, **kwargs):
    """
    PROCESSADOR ÚNICO de fidelidade.
    AGORA SÓ EXECUTA NA CRIAÇÃO DO PEDIDO, NÃO EM UPDATES!
    """
    # ⚠️ MUDANÇA CRÍTICA: Só processa na criação do pedido
    if not created:
        print(f"⏭️ Ignorando update do pedido {instance.id} - fidelidade só processa na criação")
        return

    # Prevenir processamento duplicado
    if hasattr(instance, '_fidelidade_processada'):
        return

    # IMPORTANTE: Aguardar os itens serem salvos primeiro
    # Usamos transaction.on_commit para executar DEPOIS que toda a transação for commitada
    def processar():
        print(f"\n=== PROCESSAR FIDELIDADE (APÓS ITENS) - Pedido {instance.id} ===")

        # Recarregar o pedido do banco para ter o total correto (com itens)
        pedido_atualizado = Pedido.objects.get(pk=instance.pk)
        print(f"Total do pedido (com itens): {pedido_atualizado.total}")

        if pedido_atualizado.total <= 0:
            print(f"⚠️ Pedido sem itens ou total zero, ignorando...")
            return

        with transaction.atomic():
            # Bloquear cliente para evitar race conditions
            cliente = Cliente.objects.select_for_update().get(pk=pedido_atualizado.cliente.pk)

            # ===========================================
            # PASSO 1: CALCULAR PONTOS A GANHAR
            # ===========================================
            valor_gasto = pedido_atualizado.total
            pontos_ganhos = int(valor_gasto * 10)

            print(f"Cliente: {cliente.nome}")
            print(f"Pontos atuais: {cliente.pontos}")
            print(f"Valor gasto: {valor_gasto}")
            print(f"Pontos a ganhar: {pontos_ganhos}")

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

                print(f"✓ Pontos adicionados: +{pontos_ganhos}")
                print(f"Pontos após ganho: {cliente.pontos}")

            # ===========================================
            # PASSO 3: VERIFICAR DESCONTO
            # ===========================================
            DESCONTO = Decimal("250.00")
            LIMITE = 50000
            desconto_aplicado = Decimal("0.00")

            if not MovimentacaoPontos.objects.filter(
                    pedido=pedido_atualizado,
                    tipo="uso"
            ).exists():

                if cliente.pontos >= LIMITE:
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

                    print(f"✓ DESCONTO APLICADO: {DESCONTO} MZN")
                    print(f"Pontos consumidos: -{LIMITE}")
                    print(f"Pontos restantes: {cliente.pontos}")

            # ===========================================
            # PASSO 4: ATUALIZAR PEDIDO COM DESCONTO
            # ===========================================
            if desconto_aplicado > 0:
                Pedido.objects.filter(pk=pedido_atualizado.pk).update(
                    desconto=desconto_aplicado
                )
                print(f"✓ Pedido atualizado com desconto: {desconto_aplicado}")

            # ===========================================
            # PASSO 5: ATUALIZAR GASTO ACUMULADO
            # ===========================================
            cliente.total_gasto_acumulado += Decimal(valor_gasto)
            cliente.save(update_fields=["pontos", "total_gasto_acumulado"])



            # Marcar como processado
            instance._fidelidade_processada = True

    # Executar após o commit da transação
    transaction.on_commit(processar)
