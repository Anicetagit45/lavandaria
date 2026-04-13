from datetime import timedelta
from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from decimal import Decimal



# Modelo para Lavandarias
class Lavandaria(models.Model):
    """
    Representa uma lavandaria cadastrada no sistema.
    """
    nome = models.CharField(max_length=255)
    endereco = models.TextField()
    telefone = models.CharField(max_length=20, unique=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nome


# Modelo para Funcionários
class Funcionario(models.Model):
    """
    Representa um funcionário associado a uma lavandaria.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='funcionario')
    lavandaria = models.ForeignKey(Lavandaria, on_delete=models.CASCADE, related_name='funcionarios')
    telefone = models.CharField(max_length=20, blank=True, null=True)
    grupo = models.CharField(
        max_length=255,
        choices=[('gerente', 'Gerente'), ('caixa', 'Caixa')],
        help_text="Define o grupo do usuário."
    )

    def __str__(self):
        return f"{self.user.username} - {self.grupo}"

    def save(self, *args, **kwargs):
        criar_grupos_com_permissoes()
        super().save(*args, **kwargs)

        # Associa o usuário ao grupo correto
        if self.grupo:
            grupo = Group.objects.get(name=self.grupo)
            self.user.groups.set([grupo])

        self.user.is_staff = True
        self.user.save()


# Modelo para Tipos de Artigos (Itens de Serviço)
class ItemServico(models.Model):
    """
    Representa um tipo de artigo disponível para serviço.
    """
    nome = models.CharField(max_length=255)
    preco_base = models.DecimalField(max_digits=10, decimal_places=2)
    disponivel = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Artigo"  # Nome no singular
        verbose_name_plural = "Artigos"  # Nome no plural

    def __str__(self):
        return f"{self.nome} - {self.get_preco_formatado()}"

    def get_preco_formatado(self):
        """Retorna o preço formatado em Reais"""
        return f"MT: {self.preco_base:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')


# Modelo para Serviços disponíveis na Lavandaria
class Servico(models.Model):
    """
    Representa um serviço oferecido por uma lavandaria.
    """
    lavandaria = models.ForeignKey(Lavandaria, on_delete=models.CASCADE, related_name='servicos')
    nome = models.CharField(max_length=255)
    descricao = models.TextField(blank=True, null=True)
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nome}"


# Modelo para Clientes
class Cliente(models.Model):
    """
    Representa um cliente do sistema.
    """
    nome = models.CharField(max_length=255)
    telefone = models.CharField(max_length=20, null=True, blank=True)
    endereco = models.TextField(null=True, blank=True)
    pontos = models.PositiveIntegerField(default=0)

    # Total acumulado gasto (para rastrear quando aplicar desconto)
    total_gasto_acumulado = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Total gasto acumulado para controle de descontos de fidelidade"
    )

    # Último marco de desconto aplicado
    ultimo_marco_desconto = models.PositiveIntegerField(
        default=0,
        help_text="Último múltiplo de 5000 Mts que gerou desconto"
    )

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


# Modelo para historico de pontos
class MovimentacaoPontos(models.Model):

    TIPO_CHOICES = [
        ("ganho", "Ganho"),
        ("uso", "Uso"),
        ("expiracao", "Expiração"),
        ("ajuste", "Ajuste Manual"),
    ]

    cliente = models.ForeignKey(
        "Cliente",
        on_delete=models.CASCADE,
        related_name="movimentacoes_pontos"
    )

    pedido = models.ForeignKey(
        "Pedido",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    pontos = models.IntegerField()
    criado_em = models.DateTimeField(auto_now_add=True)
    criado_por = models.ForeignKey(
        "Funcionario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.cliente} - {self.tipo} - {self.pontos} pts"

# Modelo para Pedidos
class Pedido(models.Model):
    STATUS_CHOICES = [
        ("pendente", "Pendente"),
        ("completo", "Completo"),
        ("pronto", "Pronto"),
        ("entregue", "Entregue"),
    ]

    STATUS_PAGAMENTO_CHOICES = [
        ("nao_pago", "Não pago"),
        ("parcial", "Parcial"),
        ("pago", "Pago"),
    ]

    cliente = models.ForeignKey(
        "Cliente",
        on_delete=models.CASCADE,
        related_name="pedidos"
    )

    lavandaria = models.ForeignKey(
        "Lavandaria",
        on_delete=models.CASCADE,
        related_name="pedidos"
    )

    funcionario = models.ForeignKey(
        "Funcionario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pedidos"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pendente"
    )

    criado_em = models.DateTimeField(auto_now_add=True)

    total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    desconto = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    total_pago = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    status_pagamento = models.CharField(
        max_length=20,
        choices=STATUS_PAGAMENTO_CHOICES,
        default="nao_pago"
    )

    pago = models.BooleanField(default=False)
    data_pagamento = models.DateTimeField(null=True, blank=True)

    # =============================
    # CAMPOS DE CABIDES
    # =============================
    cabides_trazidos = models.PositiveIntegerField(
        default=0,
        help_text="Quantidade de cabides trazidos pelo cliente (cada 20 cabides = 140 Mts de desconto)"
    )

    desconto_cabides = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Desconto aplicado por cabides (140 Mts por cada 20 cabides)"
    )

    # =============================
    # PROPRIEDADES FINANCEIRAS
    # =============================
    @property
    def total_final(self):
        """Calcula total com todos os descontos"""
        total = self.total or Decimal("0.00")
        descontos = (self.desconto or 0) + (self.desconto_cabides or 0)
        return max(total - Decimal(str(descontos)), Decimal("0.00"))

    @property
    def saldo(self):
        """Calcula o saldo pendente (nunca negativo)"""
        total_final = self.total_final
        total_pago = self.total_pago or Decimal("0.00")
        
        # Saldo nunca pode ser negativo
        if total_pago >= total_final:
            return Decimal("0.00")
        
        return total_final - total_pago

    @property
    def troco(self):
        if self.total_pago > self.total_final:
            return self.total_pago - self.total_final
        return Decimal("0.00")

    # =============================
    # MÉTODOS DE CÁLCULO
    # =============================
    def calcular_desconto_cabides(self):
        """Calcula o desconto baseado na quantidade de cabides trazidos"""
        if self.cabides_trazidos >= 20:
            blocos = self.cabides_trazidos // 20
            return Decimal(blocos * 140)
        return Decimal("0.00")

    def atualizar_total(self):
        """Atualiza o total baseado nos itens e recalcula pagamentos"""
        from django.db.models import Sum
        from decimal import Decimal

        novo_total = self.itens.aggregate(
            s=Sum("preco_total")
        )["s"] or Decimal("0.00")

        if self.total != novo_total:
            self.total = novo_total
            self.save(update_fields=["total"])
            self.recalcular_pagamentos()

    def recalcular_pagamentos(self):
        """
        Recalcula o status de pagamento do pedido baseado nos pagamentos registrados.
        """
        from decimal import Decimal
        from django.db.models import Sum
        from django.utils import timezone

        # Soma todos os pagamentos
        soma = self.pagamentos.aggregate(
            s=Sum("valor")
        )["s"] or Decimal("0.00")

        soma = Decimal(soma)
        total_final = self.total_final

        # Prepara novos valores
        novo_total_pago = min(soma, total_final)
        novo_status = self.status_pagamento
        novo_pago = self.pago
        nova_data = self.data_pagamento

        # Determina novo status
        if novo_total_pago <= 0:
            novo_status = "nao_pago"
            novo_pago = False
            nova_data = None
        elif novo_total_pago < total_final:
            novo_status = "parcial"
            novo_pago = False
            ultimo = self.pagamentos.order_by("-pago_em").first()
            nova_data = ultimo.pago_em if ultimo else None
        else:
            novo_status = "pago"
            novo_pago = True
            ultimo = self.pagamentos.order_by("-pago_em").first()
            nova_data = ultimo.pago_em if ultimo else timezone.now()

        # Só salva se houver mudanças
        if (self.total_pago != novo_total_pago or
                self.status_pagamento != novo_status or
                self.pago != novo_pago or
                self.data_pagamento != nova_data):
            self.total_pago = novo_total_pago
            self.status_pagamento = novo_status
            self.pago = novo_pago
            self.data_pagamento = nova_data

            self.save(update_fields=[
                "total_pago",
                "status_pagamento",
                "pago",
                "data_pagamento"
            ])

    def save(self, *args, **kwargs):
        # Calcula desconto de cabides (só se veio do formulário)
        if self.cabides_trazidos and 'update_fields' not in kwargs:
            self.desconto_cabides = self.calcular_desconto_cabides()

        super().save(*args, **kwargs)

    @transaction.atomic
    def registrar_pagamento(self, *, valor, metodo_pagamento, funcionario=None, referencia=None):
        from decimal import Decimal
        from django.core.exceptions import ValidationError

        valor = Decimal(valor)

        if valor <= 0:
            raise ValidationError("Valor do pagamento deve ser maior que zero.")

        pedido = Pedido.objects.select_for_update().get(pk=self.pk)

        PagamentoPedido.objects.create(
            pedido=pedido,
            valor=valor,
            metodo_pagamento=metodo_pagamento,
            referencia=referencia,
            criado_por=funcionario,
        )

        pedido.recalcular_pagamentos()
        return pedido

    def __str__(self):
        return f"Pedido {self.id} - {self.cliente}"

    class Meta:
        ordering = ["-criado_em"]


# Modelo para Itens do Pedido
class ItemPedido(models.Model):
    """
    Representa um item incluído em um pedido.
    """

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='itens')
    servico = models.ForeignKey(Servico, on_delete=models.SET_NULL, related_name='itens', null=True, blank=True)
    item_de_servico = models.ForeignKey(ItemServico, on_delete=models.SET_NULL, related_name='itens', null=True, blank=True, verbose_name='Artigo')
    quantidade = models.PositiveIntegerField()
    preco_total = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    descricao = models.TextField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.item_de_servico and self.quantidade:
            self.preco_total = self.item_de_servico.preco_base * self.quantidade
        else:
            self.preco_total = 0
        super().save(*args, **kwargs)
        self.pedido.atualizar_total()

    def delete(self, *args, **kwargs):
        pedido = self.pedido
        super().delete(*args, **kwargs)
        pedido.atualizar_total()

    def __str__(self):
        item_nome = self.item_de_servico.nome if self.item_de_servico else "Item Desconhecido"
        return f"{item_nome} - {self.quantidade}x - Total: {self.preco_total}"


from decimal import Decimal
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone


class PagamentoPedido(models.Model):

    METODO_PAGAMENTO_CHOICES = [
        ("numerario", "Numerario"),
        ("pos", "POS (Cartão)"),
        ("mpesa", "M-Pesa"),
        ("emola", "e-Mola"),
        ("conta_movel", "Conta Movel"),
        ("transferencia", "Transferência"),
        ("outro", "Outro"),
    ]

    pedido = models.ForeignKey(
        "Pedido",
        on_delete=models.CASCADE,
        related_name="pagamentos"
    )

    valor = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    metodo_pagamento = models.CharField(
        max_length=20,
        choices=METODO_PAGAMENTO_CHOICES
    )

    referencia = models.CharField(
        max_length=100,
        null=True,
        blank=True
    )

    pago_em = models.DateTimeField(default=timezone.now)

    criado_por = models.ForeignKey(
        "Funcionario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    criado_em = models.DateTimeField(auto_now_add=True)

    # =============================
    # VALIDAÇÕES
    # =============================
    def clean(self):
        if self.valor is None:
            raise ValidationError("O valor do pagamento é obrigatório.")

        if self.valor <= 0:
            raise ValidationError("O valor do pagamento deve ser maior que zero.")

        # NÃO bloquear pagamento maior que saldo

    def save(self, *args, **kwargs):
        """
        Save modificado para lidar com novos pedidos
        """
        # Só executa validação completa se não for um novo pedido
        if self.pedido_id and self.pedido.pk:
            self.full_clean()

        super().save(*args, **kwargs)




    # =============================
    # DELETE SEGURO
    # =============================

    @transaction.atomic
    def delete(self, *args, **kwargs):

        pedido = self.pedido
        super().delete(*args, **kwargs)
        pedido.recalcular_pagamentos()

    def __str__(self):
        return f"Pagamento {self.id} - Pedido {self.pedido.id} - {self.valor} MZN"

    class Meta:
        ordering = ["-pago_em"]


# Função para criar grupos e associar permissões
def criar_grupos_com_permissoes():
    """
    Cria grupos predefinidos (gerente, caixa) e associa as permissões específicas.
    """
    grupos_permissoes = {
        "gerente": [
            "view_funcionario",
            "add_itemservico", "change_itemservico", "delete_itemservico", "view_itemservico",
            "add_servico", "change_servico", "view_servico",
            "add_pedido", "change_pedido", "view_pedido",
            "add_cliente", "change_cliente","view_cliente",
            "add_itempedido", "view_itempedido",
            "add_pagamentopedido", "view_pagamentopedido",
        ],
        "caixa": [
            "add_pedido", "change_pedido",  "view_pedido",
            "add_cliente", "change_cliente", "view_cliente",
            "add_itempedido", "change_itempedido", "view_itempedido",
            "add_pagamentopedido", "view_pagamentopedido",
        ],
    }

    for grupo_nome, permissoes_codigos in grupos_permissoes.items():
        grupo, criado = Group.objects.get_or_create(name=grupo_nome)
        if criado:
            print(f"Grupo '{grupo_nome}' criado.")

        for permissao_codigo in permissoes_codigos:
            permissao = Permission.objects.filter(codename=permissao_codigo).first()
            if permissao:
                grupo.permissions.add(permissao)

        print(f"Permissões associadas ao grupo '{grupo_nome}': {permissoes_codigos}")


class Recibo(models.Model):
    """
    Recibo passa a ser por pagamento (não por pedido).
    Isso evita quebrar pagamentos parciais.
    """
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="recibos")
    pagamento = models.OneToOneField(PagamentoPedido, on_delete=models.PROTECT, related_name="recibo")

    total_pago = models.DecimalField(max_digits=10, decimal_places=2)
    metodo_pagamento = models.CharField(max_length=20, choices=PagamentoPedido.METODO_PAGAMENTO_CHOICES)

    emitido_em = models.DateTimeField(auto_now_add=True)
    criado_por = models.ForeignKey("Funcionario", on_delete=models.SET_NULL, blank=True, null=True)

    def save(self, *args, **kwargs):
        # NÃO mexe em status/pago aqui.
        # Status do Pedido é operacional (pendente/pronto/entregue) e pagamento é derivado dos pagamentos.
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Recibo {self.id} - Pedido {self.pedido_id} - Total: {self.total_pago}"




