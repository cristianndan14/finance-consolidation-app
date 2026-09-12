"""Los JSON schemas que se le mandan al modelo como `response_schema`.

# Por que estan escritos a mano y no derivados de los modelos de Pydantic

Gemini acepta un subconjunto de OpenAPI 3.0, no JSON Schema completo: entiende
`type`, `format`, `enum`, `items`, `nullable`, `required`, `propertyOrdering` y
poco mas. Convertir un modelo de Pydantic produce construcciones que el API
rechaza (`$defs`, `allOf`, `pattern`).

Pero la razon de fondo es otra: **las `description` de este archivo son parte del
prompt**. El modelo las lee campo por campo, y son el lugar donde entran las
reglas que mas mueven la accuracy ("el monto va positivo, el signo va aparte").
Derivarlas de los docstrings de Pydantic las volveria un efecto secundario de
otra cosa; aca se editan a proposito.

La validacion del formato la hace igual `app/schemas/llm.py` al recibir: el
schema es una guia fuerte para el modelo, no una garantia.
"""

from __future__ import annotations

from typing import Any, Final

CURRENCIES: Final = ["ARS", "USD"]

KINDS: Final = [
    "charge",
    "installment",
    "payment",
    "refund",
    "interest",
    "fee",
    "tax",
    "fx_charge",
    "adjustment",
    "unknown",
]

_AMOUNT_DESC: Final = (
    "Monto SIEMPRE positivo, con punto decimal y dos decimales, sin separador de "
    "miles y sin simbolo de moneda. El monto que en el PDF figura como 1.234,56 "
    "se escribe 1234.56. El signo NO va aca: va en direction."
)

TRANSACTIONS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "posted_date": {
                        "type": "string",
                        "description": (
                            "La fecha tal como figura en la linea, sin completar nada. "
                            "Si el resumen dice 15/03, escribir 15/03. NO inventar el "
                            "año: lo infiere el sistema con la fecha de cierre."
                        ),
                    },
                    "description_raw": {
                        "type": "string",
                        "description": (
                            "La descripcion literal del comercio tal como aparece, sin "
                            "limpiar, sin expandir abreviaturas y sin traducir."
                        ),
                    },
                    "source_line": {
                        "type": "string",
                        "description": (
                            "La linea COMPLETA del texto de entrada de la que salio esta "
                            "transaccion, copiada caracter por caracter. Es obligatoria y "
                            "se verifica contra el texto original."
                        ),
                    },
                    "amount": {"type": "string", "description": _AMOUNT_DESC},
                    "currency": {"type": "string", "enum": CURRENCIES},
                    "direction": {
                        "type": "string",
                        "enum": ["1", "-1"],
                        "description": (
                            "1 si la operacion AUMENTA la deuda (consumo, cuota, interes, "
                            "impuesto, comision). -1 si la DISMINUYE (pago, credito, "
                            "devolucion, bonificacion). Los resumenes marcan el credito de "
                            "formas distintas (CR, H, un signo menos, una columna aparte): "
                            "mirar el contexto, no solo el simbolo."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": KINDS,
                        "description": (
                            "tax para IVA, percepciones (RG 4815, RG 5463), IIBB e "
                            "impuesto de sellos. fee para comisiones y cargos "
                            "administrativos. interest para intereses de financiacion y "
                            "punitorios. installment para consumos en cuotas. fx_charge "
                            "para el cargo por conversion de moneda. payment para SU PAGO. "
                            "unknown si no se puede decidir."
                        ),
                    },
                    "source_page": {
                        "type": "integer",
                        "description": "Numero de pagina donde aparece, si el texto lo indica.",
                        "nullable": True,
                    },
                    "transaction_date": {
                        "type": "string",
                        "description": "Fecha real de la compra, si el resumen la informa aparte.",
                        "nullable": True,
                    },
                    "installment_number": {
                        "type": "integer",
                        "description": "N de CUOTA N/M. Null si no es una cuota.",
                        "nullable": True,
                    },
                    "installment_total": {
                        "type": "integer",
                        "description": "M de CUOTA N/M. Null si no es una cuota.",
                        "nullable": True,
                    },
                    "purchase_total_amount": {
                        "type": "string",
                        "description": (
                            "Monto total de la compra original en cuotas, si el resumen lo "
                            "informa. NO es el monto de la cuota. Mismo formato que amount."
                        ),
                        "nullable": True,
                    },
                    "purchase_date": {
                        "type": "string",
                        "description": "Fecha de la compra original en cuotas, si figura.",
                        "nullable": True,
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "Entre 0 y 1. Bajar el valor cuando la linea esta cortada, "
                            "ilegible o ambigua, en lugar de adivinar."
                        ),
                        "nullable": True,
                    },
                },
                "required": [
                    "posted_date",
                    "description_raw",
                    "source_line",
                    "amount",
                    "currency",
                    "direction",
                    "kind",
                ],
                "propertyOrdering": [
                    "posted_date",
                    "description_raw",
                    "source_line",
                    "amount",
                    "currency",
                    "direction",
                    "kind",
                    "source_page",
                    "transaction_date",
                    "installment_number",
                    "installment_total",
                    "purchase_total_amount",
                    "purchase_date",
                    "confidence",
                ],
            },
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Problemas encontrados: una pagina que parece cortada, una columna "
                "ilegible, un total que no se entiende. Preferir avisar aca antes que "
                "completar con datos inventados."
            ),
        },
    },
    "required": ["transactions"],
    "propertyOrdering": ["transactions", "warnings"],
}


STATEMENT_HEADER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "statements": {
            "type": "array",
            "description": (
                "Un elemento POR MONEDA. Un resumen argentino trae una seccion en "
                "pesos y otra en dolares, con totales propios cada una. Si solo hay "
                "pesos, devolver un solo elemento."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string", "enum": CURRENCIES},
                    "closing_date": {
                        "type": "string",
                        "description": "Fecha de cierre del resumen, como figura.",
                        "nullable": True,
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Fecha de vencimiento del pago.",
                        "nullable": True,
                    },
                    "previous_balance": {
                        "type": "string",
                        "description": f"Saldo anterior. {_AMOUNT_DESC}",
                        "nullable": True,
                    },
                    "payments_credits": {
                        "type": "string",
                        "description": f"Pagos y creditos del periodo. {_AMOUNT_DESC}",
                        "nullable": True,
                    },
                    "new_charges": {
                        "type": "string",
                        "description": (
                            "Consumos del periodo: SOLO lo nuevo, sin el saldo anterior. "
                            "Suele figurar como Consumos del periodo, Compras del mes o "
                            "Debitos. Es el campo mas importante del header. "
                            f"{_AMOUNT_DESC}"
                        ),
                        "nullable": True,
                    },
                    "total_due": {
                        "type": "string",
                        "description": (
                            "Total a pagar de este resumen, que INCLUYE el saldo anterior. "
                            f"{_AMOUNT_DESC}"
                        ),
                        "nullable": True,
                    },
                    "minimum_payment": {
                        "type": "string",
                        "description": f"Pago minimo. {_AMOUNT_DESC}",
                        "nullable": True,
                    },
                    "card_last4": {
                        "type": "string",
                        "description": "Ultimos 4 digitos de la tarjeta.",
                        "nullable": True,
                    },
                    "issuer_name": {
                        "type": "string",
                        "description": "Banco o emisor (Galicia, Santander, BBVA, Naranja X).",
                        "nullable": True,
                    },
                },
                "required": ["currency"],
                "propertyOrdering": [
                    "currency",
                    "closing_date",
                    "due_date",
                    "previous_balance",
                    "payments_credits",
                    "new_charges",
                    "total_due",
                    "minimum_payment",
                    "card_last4",
                    "issuer_name",
                ],
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["statements"],
    "propertyOrdering": ["statements", "warnings"],
}


def merchants_schema(category_slugs: list[str]) -> dict[str, Any]:
    """El schema del enriquecimiento, con las categorias del usuario adentro.

    Se arma en runtime porque la lista de categorias no es fija: cada usuario
    puede crear las suyas. Ponerla como `enum` y no como texto libre es lo que
    impide que el modelo invente una categoria que despues no existe en la base.
    """
    return {
        "type": "object",
        "properties": {
            "merchants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "raw_key": {
                            "type": "string",
                            "description": (
                                "La clave tal como se te pasó, copiada exacta. Es lo que "
                                "permite aparear tu respuesta con la pregunta."
                            ),
                        },
                        "canonical_name": {
                            "type": "string",
                            "description": (
                                "El nombre limpio del comercio, como lo diría una persona: "
                                "Spotify, Coto, YPF. Sin códigos, sin sucursal, sin el "
                                "prefijo del procesador de pagos."
                            ),
                        },
                        "category_slug": {
                            "type": "string",
                            "enum": category_slugs,
                            "description": (
                                "La categoría que mejor encaja. Si ninguna encaja de verdad, "
                                "omitilo: una categoría inventada ensucia el análisis más de "
                                "lo que ayuda."
                            ),
                            "nullable": True,
                        },
                        "confidence": {"type": "number", "nullable": True},
                    },
                    "required": ["raw_key", "canonical_name"],
                    "propertyOrdering": [
                        "raw_key",
                        "canonical_name",
                        "category_slug",
                        "confidence",
                    ],
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["merchants"],
        "propertyOrdering": ["merchants", "warnings"],
    }
