"""Infraestrutura: implementações concretas (Redis, fila, locks) das interfaces do Core.

Regra: o Core NÃO importa este pacote. A infraestrutura depende do Core, nunca o contrário.
Redis é uma camada de coordenação EFÊMERA: a fonte da verdade é o banco relacional.
"""
