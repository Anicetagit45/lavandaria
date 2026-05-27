# Modelo para Clientes
class Cliente(models.Model):
    """
       Representa um cliente do sistema.
    """
    nome = models.CharField(max_length=255, db_index=True)  # Adicionado db_index
    telefone = models.CharField(max_length=20, null=True, blank=True, db_index=True)  # Adicionado db_index
    endereco = models.TextField(null=True, blank=True)
    pontos = models.PositiveIntegerField(default=0, db_index=True)  # Adicionado db_index
    criado_em = models.DateTimeField(auto_now_add=True, db_index=True)  # Adicionado campo e índice

    # Total acumulado gasto (para rastrear quando aplicar desconto)
    total_gasto_acumulado = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Total gasto acumulado para controle de descontos de fidelidade",
        db_index=True  # Adicionado db_index
    )

    # Último marco de desconto aplicado
    ultimo_marco_desconto = models.PositiveIntegerField(
        default=0,
        help_text="Último múltiplo de 5000 Mts que gerou desconto"
    )

    class Meta:
        indexes = [
            models.Index(fields=['nome']),
            models.Index(fields=['telefone']),
            models.Index(fields=['pontos']),
            models.Index(fields=['criado_em']),
            models.Index(fields=['total_gasto_acumulado']),
            # Índices compostos para consultas comuns
            models.Index(fields=['nome', 'pontos']),
            models.Index(fields=['criado_em', 'pontos']),
        ]
        ordering = ['-criado_em']  # Ordenação padrão

    # ... resto dos métodos existentes (pontos_validos, verificar_desconto_fidelidade, expirar_pontos, __str__)

    def pontos_validos(self):
        tres_meses_atras = timezone.now() - timedelta(days=90)

        pontos_ganhos = self.movimentacoes_pontos.filter(
            tipo="ganho",
            criado_em__gte=tres_meses_atras
        ).aggregate(total=Sum("pontos"))["total"] or 0

        pontos_usados = abs(
            self.movimentacoes_pontos.filter(
                tipo="uso"
            ).aggregate(total=Sum("pontos"))["total"] or 0
        )

        return max(0, pontos_ganhos + pontos_usados)

    # models.py - Classe Cliente - Método modificado

    # models.py - Classe Cliente - Versão Produção
    from decimal import Decimal



    def verificar_desconto_fidelidade(self, valor_pago):
        """
        Verifica se o cliente atingiu os critérios de fidelidade
        e aplica desconto se necessário, consumindo pontos.
        """
        LIMITE_GASTO = Decimal("5000.00")
        DESCONTO = Decimal("250.00")
        PONTOS_LIMITE = 50000

        desconto = Decimal("0.00")

        # Atualiza gasto acumulado e gera pontos **uma vez**
        pontos_ganhos = int(valor_pago * 10)
        self.pontos += pontos_ganhos
        self.total_gasto_acumulado += Decimal(valor_pago)

        # Aplica desconto baseado em pontos
        if self.pontos >= PONTOS_LIMITE:
            desconto += DESCONTO
            self.pontos -= PONTOS_LIMITE  # consome os pontos usados

        # Aplica desconto baseado em gasto acumulado
        if self.total_gasto_acumulado >= LIMITE_GASTO:
            desconto += DESCONTO
            self.total_gasto_acumulado -= LIMITE_GASTO  # consome gasto

        self.save(update_fields=["pontos", "total_gasto_acumulado"])

        return desconto


    def expirar_pontos(self):
        from django.utils import timezone
        from datetime import timedelta
        from django.db.models import Sum
        from .models import MovimentacaoPontos

        limite = timezone.now() - timedelta(days=90)

        # Pontos ganhos há mais de 90 dias
        pontos_antigos = self.movimentacoes_pontos.filter(
            tipo="ganho",
            criado_em__lt=limite
        ).aggregate(total=Sum("pontos"))["total"] or 0

        if pontos_antigos <= 0:
            return

        # Evitar expirar duas vezes os mesmos pontos
        pontos_ja_expirados = abs(
            self.movimentacoes_pontos.filter(
                tipo="expiracao"
            ).aggregate(total=Sum("pontos"))["total"] or 0
        )

        pontos_para_expirar = pontos_antigos - pontos_ja_expirados

        if pontos_para_expirar <= 0:
            return

        # Atualizar saldo do cliente
        self.pontos = max(0, self.pontos - pontos_para_expirar)
        self.save(update_fields=["pontos"])

        # Registrar movimentação
        MovimentacaoPontos.objects.create(
            cliente=self,
            tipo="expiracao",
            pontos=-pontos_para_expirar
        )

    def __str__(self):
        return f"{self.nome} - {self.telefone}"
