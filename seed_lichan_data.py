#!/usr/bin/env python3
"""
Seed Lichan customers and products into fourdist-dev via direct Lambda invocation.

Usage:
    python seed_lichan_data.py
    python seed_lichan_data.py --stage prod    # target prod
    python seed_lichan_data.py --only customers
    python seed_lichan_data.py --only products
"""
import argparse
import json
import logging
import sys

import boto3

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENTERPRISE_ID = "enterprise-0658d531-21f9-48ad-8ed7-6a9fb65a91c0"
COMPANY_ID    = "company-lichan"
USER_ID       = "a8c1e360-8071-70c6-2465-fe534058050e"
WAREHOUSE_ID  = "warehouse-lichan"

AUTH_USER = {
    "userId":       USER_ID,
    "enterpriseId": ENTERPRISE_ID,
    "companyId":    COMPANY_ID,
    "roleId":       "role-master-admin",
    "roleLevel":    1,
    "permissions":  ["admin:all"],
}

FUNCTION_SUFFIXES = {
    "dev":  {"customer": "CustomerFunction-2Tfdyup6KDnx", "product": "ProductFunction-BZOAJovsGxoA"},
    "qa":   {"customer": "CustomerFunction-Cmz1eF5bCs3B",  "product": "ProductFunction-vR04kPkQluvm"},
    "prod": {"customer": "CustomerFunction-0Ej4WeXxJB4w",  "product": "ProductFunction-lKS5OmQIYKxg"},
}

# ---------------------------------------------------------------------------
# Customer data (from CUSTOMERS.xlsx)
# ---------------------------------------------------------------------------

CUSTOMERS = [
    {
        "ruc":     "20477721177",
        "name":    "ALFACHEM EIRL",
        "email":   "alfachemeirl@gmail.com",
        "address": "U.C. C-03A NRO. PREDIO VALDIVIA LA LIBERTAD - TRUJILLO - HUANCHACO",
        "city":    "HUANCHACO",
        "country": "PERU",
    },
    {
        "ruc":     "20142027578",
        "name":    "EXIQUIM S.A.C",
        "email":   "exiquimsac@gmail.com",
        "address": "CAR.HUANCHACO NRO. S/N SEC. VALDIVIA ALTA LA LIBERTAD - TRUJILLO - HUANCHACO",
        "city":    "HUANCHACO",
        "country": "PERU",
    },
    {
        "ruc":     "20601038154",
        "name":    "JOMATH COLORS E.I.R.L.",
        "email":   None,
        "address": "JR. CONTRAALMIRANTE MONTERO NRO. 1488 LIMA - LIMA - SURQUILLO",
        "city":    "SURQUILLO",
        "country": "PERU",
    },
    {
        "ruc":     "20462335653",
        "name":    "QUIMICA REGASA S.A.C.",
        "email":   None,
        "address": "AV. MARCO PUENTE LLANOS MZA. A LOTE. 4A URB. BARBADILLO LIMA - LIMA - ATE",
        "city":    "ATE",
        "country": "PERU",
    },
    {
        "ruc":     "20614658658",
        "name":    "CURTEX CHEMICAL E.I.R.L.",
        "email":   None,
        "address": "JR. AREQUIPA NRO. 3487 URB. PERU LIMA - LIMA - SAN MARTIN DE PORRES",
        "city":    "SAN MARTIN DE PORRES",
        "country": "PERU",
    },
    {
        "ruc":     "20503706823",
        "name":    "HIDROQUIMICA COLOR E.I.R.L.",
        "email":   "comercializacion@hidroquimica.com.pe",
        "address": "JR. IGNACIO COSSIO NRO. 1445 LIMA - LIMA - LA VICTORIA",
        "city":    "LA VICTORIA",
        "country": "PERU",
    },
    {
        "ruc":     "20108878364",
        "name":    "ARTESANIA LANERA ANDINA S.A.",
        "email":   None,
        "address": "AV. SAN ALFONSO NRO. 399 URB. CENTRO POBLADO SANTA CLAR LIMA - LIMA - ATE",
        "city":    "ATE",
        "country": "PERU",
    },
    {
        "ruc":     "20494741726",
        "name":    "HILOS Y COLORES S.A.C.",
        "email":   None,
        "address": "MZA. L2 LOTE. 01 A.H. COVADONGA AYACUCHO - HUAMANGA - AYACUCHO",
        "city":    "AYACUCHO",
        "country": "PERU",
    },
    {
        "ruc":     "20605573127",
        "name":    "QUIMICA ALLENDE S.A.C.",
        "email":   None,
        "address": "CAL.LAS GALITAS NRO. 540 URB. INCA MANCO CAPAC LIMA - LIMA - SAN JUAN DE LURIGANCHO",
        "city":    "SAN JUAN DE LURIGANCHO",
        "country": "PERU",
    },
    {
        "ruc":     "20459787616",
        "name":    "GARAMENDI & CIA S.R.L.",
        "email":   None,
        "address": "AV. DEL EJERCITO NRO. 254 MARIANO MELGAR LIMA - LIMA - VILLA MARIA DEL TRIUNFO",
        "city":    "VILLA MARIA DEL TRIUNFO",
        "country": "PERU",
    },
    {
        "ruc":     "20501820025",
        "name":    "QUIMICA MARTELL S.A.C.",
        "email":   None,
        "address": "CAL.SANTA ANA MZA. E LOTE. 51-B CHACRA CERRO LIMA - LIMA - COMAS",
        "city":    "COMAS",
        "country": "PERU",
    },
]

# ---------------------------------------------------------------------------
# Product data (from PRODUCTOS_LICHAN.xlsx)
# Unit mapping: KILOGRAMO -> kg, UNIDAD -> pcs
# PEN-priced products: base_price = None (not stored), avg_price_pen noted in metadata
# ---------------------------------------------------------------------------

UNIT_MAP = {"KILOGRAMO": "kg", "UNIDAD": "pcs"}

PRODUCTS = [
    ("NL-NON-001", "NONYL FENOL DE 09 MOLES",                                   "IMPORTADO", "kg",  17.0,    None),
    ("NL-ACI-001", "ACIDO ACETICO GLACIAL 99% (08 IBC X 1050 KGS C/U)",          "IMPORTADO", "kg",  0.7667,  None),
    ("NL-ACI-002", "ACIDO ACETICO GLACIAL 99%",                                   "IMPORTADO", "kg",  0.9838,  None),
    ("NL-GLY-001", "GLYOXAL AL 40%",                                              "IMPORTADO", "kg",  1.72,    None),
    ("NL-PER-001", "PEROXIDO DE HIDROGENO AL 50%",                                "IMPORTADO", "kg",  0.7388,  None),
    ("NL-SAL-001", "BENZOATO DE SODIO",                                           "IMPORTADO", "kg",  1.08,    None),
    ("NL-ACI-003", "ACIDO ESTEARICO",                                             "IMPORTADO", "kg",  1.3914,  None),
    ("NL-FOS-001", "TRIPOLIFOSFATO DE SODIO",                                     "IMPORTADO", "kg",  1.1,     None),
    ("NL-SUL-001", "SULFATO DE COBRE PENTAHIDRATADO",                             "IMPORTADO", "kg",  2.78,    None),
    ("NL-VAE-001", "VAE F104",                                                    "IMPORTADO", "kg",  0.805,   None),
    ("NL-ACI-004", "ACIDO ACETICO GLACIAL 99% (08 IBC X 1050 KGS C/U) v2",       "IMPORTADO", "kg",  0.77,    None),
    ("NL-POT-001", "POTASA CAUSTICA",                                             "IMPORTADO", "kg",  1.12,    None),
    ("NL-POT-002", "POTASA CAUSTICA AL 90% (BOLSAS X 25 KGS ESCAMAS)",           "IMPORTADO", "kg",  1.05,    None),
    ("NL-POT-003", "POTASA CAUSTICA AL 90% (BOLSAS POR 25 KGS)",                 "IMPORTADO", "kg",  1.05,    None),
    ("NL-ACI-005", "ACIDO ESTEARICO T.P",                                         "IMPORTADO", "kg",  1.67,    None),
    ("NL-ACI-006", "ACIDO ACETICO GLACIAL 99% (IBC X 1050KG)",                   "IMPORTADO", "kg",  0.79,    None),
    ("NL-AMI-001", "AMINO ETIL ETANOL AMINO (AEEA) 8 CILINDROS X 210KG",         "IMPORTADO", "kg",  1.5,     None),
    ("NL-ACI-007", "ACIDO ACETICO GLACIAL 99% (04 IBC X 1050 KGS)",              "IMPORTADO", "kg",  0.82,    None),
    ("NL-POT-004", "POTASA CAUSTICA EN ESCAMAS AL 90%",                          "IMPORTADO", "kg",  1.04,    None),
    ("NL-OPT-001", "BLANQUEADOR OPTICO BAC",                                      "IMPORTADO", "kg",  25.6,    None),
    ("NL-POL-001", "POLIETILENGLICOL 3350 - MATPHARM QH 533",                    "CHINA",     "kg",  0.76,    None),
    ("NL-GLU-001", "GLUTARALDEHIDO AL 50% (TAMBORES DE PLASTICO 9 X 220)",       "IMPORTADO", "kg",  1.38,    None),
    ("NL-ACI-008", "ACIDO ACETICO GLACIAL 99% (BIDON X 30KG)",                   "IMPORTADO", "kg",  0.855,   None),
    ("NL-AMI-002", "AMINA ETIL ETANOL AMINA (AEEA)",                              "IMPORTADO", "kg",  1.5,     None),
    ("NL-GLU-002", "GLUTARALDEHIDO AL 50%",                                       "IMPORTADO", "kg",  2.2,     None),
    ("NL-POT-005", "POTASA CAUSTICA EN ESCAMAS AL 90% v2",                       "IMPORTADO", "kg",  1.04,    None),
    ("NL-AZU-001", "AZUFRE MICRONIZADO",                                          "IMPORTADO", "kg",  0.44,    None),
    ("NL-POT-006", "POTASA CAUSTICA EN ESCAMAS AL 90% v3",                       "IMPORTADO", "kg",  1.04,    None),
    ("NL-NON-003", "NONYL FENOL DE 06 MOLES",                                    "IMPORTADO", "kg",  1.96,    None),
    ("NL-GLY-002", "GLYOXAL AL 40% BASF (14 TAMBORES X 260 KGS + 01 X 212)",    "ALEMANIA",  "kg",  0.5,     None),
    ("NL-FOS-002", "TRIPOLIFOSFATO DE SODIO GRADO TECNICO",                      "IMPORTADO", "kg",  0.9167,  None),
    ("NL-OPT-002", "BLANCO OPTICO ACRILICO BC",                                  "IMPORTADO", "kg",  28.5,    None),
    ("NL-SIL-001", "XIAMETER 8803 / ACEITE DE SILICONA",                         "EE.UU.",    "kg",  1.7,     None),
    ("NL-NIT-001", "NITRITO DE SODIO BASF ALEMANIA",                             "ALEMANIA",  "kg",  1.79,    None),
    ("NL-CLO-001", "CLORURO DE BARIO DIHIDRATADO",                               "IMPORTADO", "kg",  0.29,    None),
    ("NL-QUI-001", "ETIL HEXANOL",                                                "IMPORTADO", "kg",  1.75,    None),
    ("NL-AMI-003", "TRIETANOLAMINA 99% / TEA 99%",                               "IMPORTADO", "kg",  1.41,    None),
    ("NL-NON-004", "NONYL FENOL DE 06 MOLES (03 TAMBORES X 215KGS)",             "IMPORTADO", "kg",  1.98,    None),
    ("NL-ANH-001", "ANHIDRIDO MALEICO (BIG BAG X 600 KG)",                       "IMPORTADO", "kg",  1.0,     None),
    ("NL-SUV-001", "SUAVIZANTE CATIONICO EN ESCAMAS",                             "IMPORTADO", "kg",  3.18,    None),
    ("NL-SIL-002", "ACEITE DE SILICONA - XIAMETER 8040 (3 CILINDROS X 190KG)",  "EE.UU.",    "kg",  2.0,     None),
    ("NL-FLO-001", "FLOCULANTE ANIONICO PAM (POLIACRILAMIDA)",                   "IMPORTADO", "kg",  1.1,     None),
    ("NL-FLO-002", "FLOCULANTE ANIONICO PAM (POLIACRILAMIDA) v2",                "IMPORTADO", "kg",  1.1,     None),
    ("NL-BAR-001", "BARDAC 208 TAMBOR X 186 KGS Y 01 X 111 KGS",                "IMPORTADO", "kg",  1.85,    None),
    ("NL-QUI-002", "DICIANDIAMIDA 99%",                                           "IMPORTADO", "kg",  1.75,    None),
    ("NL-POT-007", "POTASA CAUSTICA ESCAMAS AL 90%",                             "IMPORTADO", "kg",  1.04,    None),
    ("NL-SAL-002", "GLUCONATO DE SODIO",                                          "IMPORTADO", "kg",  0.5,     None),
    ("NL-SOL-001", "ISOPAR V FLUID",                                              "EE.UU.",    "kg",  1.75,    None),
    ("NL-CLO-002", "CLORURO DE BARIO DIHIDRATADO 99%",                           "IMPORTADO", "kg",  0.3333,  None),
    ("NL-ACI-009", "VERDE ACIDO V 333%",                                          "IMPORTADO", "kg",  32.0,    None),
    ("NL-QUI-003", "TRIPOLIFOFASTO DE SODIO GRADO TECNICO v2",                   "IMPORTADO", "kg",  0.8,     None),
    ("NL-ANH-002", "ANHIDRIDO MALEICO",                                           "IMPORTADO", "kg",  1.28,    None),
    ("NL-RES-001", "TOFA F1",                                                     "FINLANDIA", "kg",  1.0,     None),
    ("NL-POT-008", "POTASA CAUSTICA AL 90%",                                     "IMPORTADO", "kg",  1.075,   None),
    ("NL-ACI-010", "VERDE ACIDO V 333% v2",                                       "IMPORTADO", "kg",  29.0,    None),
    ("NL-FLO-003", "FLOCULANTE ANIONICO (POLIACRILAMIDA)",                        "IMPORTADO", "kg",  1.1,     None),
    ("NL-POL-002", "PROPILENGLICOL USP",                                          "IMPORTADO", "kg",  1.65,    None),
    ("NL-TEN-001", "PLURAFAC LF 403",                                             "ALEMANIA",  "kg",  0.9,     None),
    ("NL-AMI-004", "ETILENDIAMINA",                                               "IMPORTADO", "kg",  1.68,    None),
    ("NL-AMI-005", "ETILENDIAMINA / EDA",                                         "IMPORTADO", "kg",  1.68,    None),
    ("NL-COL-001", "NEGRO AL CROMO T",                                            "IMPORTADO", "kg",  11.5,    None),
    ("NL-ACI-011", "ACIDO ACEITO",                                                "IMPORTADO", "kg",  1.19,    None),
    ("NL-QUI-004", "EDTA TETRASODICO - DISOLVINE NA",                            "PAISES BAJOS","kg", 1.1,    None),
    ("NL-QUI-005", "TAMBOR X 186 KGS Y 01 X 104 KGS",                           "IMPORTADO", "kg",  1.85,    None),
    ("NL-BAR-002", "BARDOC 208 TAMBOR X 186 KGS Y 01 X 104 KGS",                "IMPORTADO", "kg",  1.85,    None),
    ("NL-ACI-012", "AZUL BRILANTE ACIDO 2 R 200% C.I 62",                        "IMPORTADO", "kg",  52.0,    None),
    ("NL-CAR-001", "CARBON ACTIVO 6X 12",                                         "IMPORTADO", "kg",  2.54,    None),
    ("NL-ACI-013", "ACIDO ACETICO",                                               "IMPORTADO", "kg",  1.19,    None),
    ("NL-ACI-014", "AZUL MARINO ACIDO 5R 200% C.I 113",                          "IMPORTADO", "kg",  20.0,    None),
    ("NL-SIL-003", "ACEITE DE SILICONA XIAMETER 8803 DOW CHEMICAL",              "EE.UU.",    "kg",  2.59,    None),
    ("NL-AMI-006", "TRIETILETRAMINA",                                             "IMPORTADO", "kg",  2.45,    None),
    ("NL-AMI-007", "TRIETILENTETRAMINA / TETA",                                  "IMPORTADO", "kg",  2.45,    None),
    # NL-COL-002: PEN price — skip unit price
    ("NL-COL-002", "AZUL MARINO 5R AL 140%",                                     "IMPORTADO", "kg",  None,    "Precio prom. historico: S/. 70.86/kg (sujeto a variacion de mercado)"),
    ("NL-POL-003", "POLYPOL E 400 / PEG 400 (TAMBORES X 230 KGS)",              "IMPORTADO", "kg",  2.0,     None),
    ("NL-GLY-003", "GLYOXAL AL 40% (TAMBORES DE 260KG)",                         "IMPORTADO", "kg",  0.83,    None),
    ("NL-FOS-003", "HEXAMETAFOSFATO DE SODIO",                                    "IMPORTADO", "kg",  2.2,     None),
    ("NL-DIS-001", "AZUL DISPERSO R 150% C.I 56",                                "IMPORTADO", "kg",  5.65,    None),
    # NL-ACI-015: PEN price — skip unit price
    ("NL-ACI-015", "ACIDO CITRICO",                                               "IMPORTADO", "kg",  None,    "Precio prom. historico: S/. 2.40/kg (sujeto a variacion de mercado)"),
    ("NL-DIS-002", "COLORANTE NEGRO DISPERSO EX SF 300%",                        "IMPORTADO", "kg",  2.1,     None),
    ("NL-SIL-004", "ACEITE DE SILICONA XIAMETER 8040",                           "EE.UU.",    "kg",  2.1,     None),
    ("NL-CLO-003", "CLORURO BARIO DIHIDRATADO",                                  "IMPORTADO", "kg",  0.35,    None),
    ("NL-QUI-006", "NEARCHEL NCH (INHIBIDOR DE CORROSION)",                      "PAISES BAJOS","kg", 0.9,    None),
    ("NL-CLO-004", "CLORURO DE BARIO DIHIDRATDO 99%",                            "IMPORTADO", "kg",  0.3,     None),
    ("NL-SAL-003", "BICROMATO DE SODIO",                                          "IMPORTADO", "kg",  5.765,   None),
    ("NL-DIS-003", "ROJO DISPERSO FBL 200% C.I 60",                              "IMPORTADO", "kg",  5.65,    None),
    ("NL-DIS-004", "AZUL MARINO DISPERSO EX SF 300%",                            "IMPORTADO", "kg",  2.8,     None),
    ("NL-COL-003", "NEGRO AL CROMO",                                              "IMPORTADO", "kg",  11.0,    None),
    ("NL-GLU-003", "GLUTARALDEHIDO AL 50% v2",                                   "IMPORTADO", "kg",  1.35,    None),
    ("NL-ACI-016", "ACIDO ACETICO GLACIAL (IBC)",                                "IMPORTADO", "pcs", 1.4,     None),
    ("NL-CER-001", "CERA POLIETILENICA OXIDADA",                                  "IMPORTADO", "kg",  2.5,     None),
    # NL-TEN-002: PEN price — skip unit price
    ("NL-TEN-002", "ALKONAT LA 230",                                              "IMPORTADO", "kg",  None,    "Precio prom. historico: S/. 4.50/kg (sujeto a variacion de mercado)"),
    ("NL-COL-004", "NEGRO REACTIVO WNN",                                          "IMPORTADO", "kg",  2.32,    None),
    ("NL-COL-005", "AZUL MARINO 5R 200% C.I 113",                                "IMPORTADO", "kg",  22.5,    None),
    ("NL-NIT-002", "NITRITO DE SODIO",                                            "IMPORTADO", "kg",  1.0,     None),
    ("NL-FOS-004", "HEXAMETAFOSFATO DE SODIO GRADO ALIMENTICIO",                 "IMPORTADO", "kg",  2.2,     None),
    ("NL-GLY-004", "GLYOXAL AL 40% BASF ALEMANIA (TAMBORES X 260 KG)",          "ALEMANIA",  "kg",  0.83,    None),
    ("NL-AMI-008", "DIETANOLAMINA AL 85% DEA (CILINDROS X 230 KGS)",             "IMPORTADO", "kg",  0.88,    None),
    ("NL-FOS-005", "100 TRIPOLIFOSFATO DE SODIO",                                "IMPORTADO", "kg",  0.8,     None),
    ("NL-DIS-005", "TURQUEZA DISPERSO GL 200% C.I 60",                           "IMPORTADO", "kg",  7.8,     None),
    ("NL-COL-006", "AZUL ROYAL TERASIL WEL",                                     "SUIZA",     "kg",  7.5,     None),
    ("NL-COL-007", "TURQUEZA SUNCRON GGS 200%",                                  "INDIA",     "kg",  7.5,     None),
    ("NL-ACI-017", "NARANJA ACIDO II",                                            "IMPORTADO", "kg",  6.8,     None),
    ("NL-DIS-006", "RUBI DISPERSO 2 GFL 200%",                                   "IMPORTADO", "kg",  3.3,     None),
    ("NL-SIL-005", "XIAMETER 8040 / ACEITE DE SILICONA",                         "EE.UU.",    "kg",  2.0,     None),
    ("NL-ALC-001", "ALCOHOL ISOPROPILICO / IPA",                                  "IMPORTADO", "kg",  1.1,     None),
    ("NL-QUI-007", "JABON CONC. DE 09",                                           "IMPORTADO", "kg",  2.5,     None),
    ("NL-DIS-007", "TURQUEZA DISPERSO PBGF 200% C.I 60",                         "IMPORTADO", "kg",  5.65,    None),
    ("NL-PER-002", "PEROXIDO DE HIDROGENO AL 50% (08 BIDONES X 30 KGS)",         "IMPORTADO", "kg",  0.52,    None),
    ("NL-DIS-008", "DISPERSANTE EN POLVO",                                        "IMPORTADO", "kg",  0.975,   None),
    ("NL-POL-004", "PEG 400",                                                     "IMPORTADO", "kg",  1.0,     None),
    ("NL-DIS-009", "NEGRO DISPERSO EX SF 300%",                                  "IMPORTADO", "kg",  2.1,     None),
    ("NL-QUI-008", "CLORITO DE SODIO AL 80%",                                    "IMPORTADO", "kg",  2.0,     None),
    ("NL-ACI-018", "ACIDO ACETICO GLACIAL (03 X 30 KGS C/U)",                   "IMPORTADO", "kg",  1.0,     None),
    ("NL-QUI-009", "HIPOCLORITO DE CALCIO AL 65-70%",                            "IMPORTADO", "kg",  1.85,    None),
    ("NL-DIS-010", "ROJO DISPERSO SER C.I 50",                                   "IMPORTADO", "kg",  3.3,     None),
    ("NL-DIS-011", "AMARILLO DISPERSO P 4G C.I 211",                             "IMPORTADO", "kg",  3.3,     None),
    # NL-SRV-001: PEN price — skip unit price
    ("NL-SRV-001", "CERTIFICADO POZO TIERRA",                                    "IMPORTADO", "pcs", None,    "Precio prom. historico: S/. 240.00/und (sujeto a variacion de mercado)"),
    ("NL-COL-008", "DIANIX RUBINE XF-2",                                         "SUIZA",     "kg",  3.5,     None),
    ("NL-COL-009", "TERASIL NEGRO W SNE",                                         "SUIZA",     "kg",  3.5,     None),
    ("NL-DIS-012", "ROJO DISPERSO C.I 73",                                        "IMPORTADO", "kg",  4.0,     None),
    ("NL-ACI-019", "ACIDO PARDO 75",                                              "IMPORTADO", "kg",  1.0,     None),
    ("NL-COL-010", "TERASIL AMARILLO W",                                          "SUIZA",     "kg",  3.5,     None),
    ("NL-DIS-013", "DISPERSE RUBI TXF",                                           "IMPORTADO", "kg",  3.5,     None),
    ("NL-DIS-014", "AMARILLO DISPERSO 6GS",                                       "IMPORTADO", "kg",  4.0,     None),
    ("NL-SAL-004", "CITRATO DE SODIO",                                            "IMPORTADO", "kg",  0.85,    None),
    ("NL-COL-011", "CATIONICO AMARILLO X 8GL 250%",                              "IMPORTADO", "kg",  3.5,     None),
    ("NL-SUL-002", "TIOSULFATO DE SODIO",                                         "IMPORTADO", "kg",  0.45,    None),
    ("NL-COL-012", "FORON RUBI SWF",                                              "SUIZA",     "kg",  3.5,     None),
    ("NL-ACI-020", "ACIDDO ACETICO GLACIAL",                                      "IMPORTADO", "kg",  1.1,     None),
    ("NL-COL-013", "VERDE MALAQUITA",                                             "IMPORTADO", "kg",  4.0,     None),
    ("NL-POT-009", "POTASA CAUSTICA / HIDROXIDO DE POTASIO ESCAMAS AL 90%",     "IMPORTADO", "kg",  1.15,    None),
    ("NL-COL-014", "BASICO ROJO XGRL 250%",                                      "IMPORTADO", "kg",  3.5,     None),
    ("NL-DIS-015", "VIOLETA DISPERSO FBL",                                        "IMPORTADO", "kg",  4.0,     None),
    ("NL-QUI-010", "SUNCRON BLACK S XF WN ECO",                                  "INDIA",     "kg",  3.5,     None),
]

# ---------------------------------------------------------------------------
# Lambda invoker
# ---------------------------------------------------------------------------

_lambda_client = None

def _get_lambda():
    global _lambda_client
    if not _lambda_client:
        _lambda_client = boto3.client("lambda", region_name="us-west-2")
    return _lambda_client


def _invoke(function_name: str, method: str, path: str, body: dict = None, query: dict = None) -> dict:
    event = {
        "httpMethod":             method,
        "path":                   path,
        "body":                   json.dumps(body or {}),
        "pathParameters":         {},
        "queryStringParameters":  query or {},
        "requestContext": {
            "stage":        "dev",
            "resourcePath": path,
            "authorizer": {
                "claims": {"sub": AUTH_USER["userId"], "email": ""}
            },
        },
    }
    # Inject auth_user for direct Lambda invocation (bypass API Gateway authorizer)
    event["_auth_user"] = AUTH_USER

    resp = _get_lambda().invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    raw = resp["Payload"].read()
    result = json.loads(raw)

    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {result}")

    status = result.get("statusCode", 200)
    try:
        body_out = json.loads(result.get("body", "{}"))
    except Exception:
        body_out = {}

    if status >= 400:
        msg = body_out.get("error") or body_out.get("message") or str(body_out)[:300]
        raise RuntimeError(f"HTTP {status}: {msg}")

    return body_out


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def seed_customers(customer_fn: str):
    logger.info(f"=== Seeding {len(CUSTOMERS)} customers ===")
    ok = 0
    for c in CUSTOMERS:
        payload = {
            "name":           c["name"],
            "documentNumber": c["ruc"],
            "documentType":   "RUC",
            "customerType":   "business",
            "category":       "regular",
            "isActive":       True,
            "companyId":      COMPANY_ID,
        }
        if c.get("email"):
            payload["email"] = c["email"]
        if c.get("address"):
            payload["address"] = c["address"]
        if c.get("city"):
            payload["city"] = c["city"]
        if c.get("country"):
            payload["country"] = c["country"]

        try:
            resp = _invoke(customer_fn, "POST", "/core/customers", body=payload)
            cid = (resp.get("data") or resp).get("customerId", "?")
            logger.info(f"  ✅ {c['name']} → {cid}")
            ok += 1
        except RuntimeError as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower() or "409" in str(e):
                logger.info(f"  ⏭  {c['name']} already exists — skipping")
                ok += 1
            else:
                logger.error(f"  ❌ {c['name']}: {e}")

    logger.info(f"Customers: {ok}/{len(CUSTOMERS)} done\n")


def seed_products(product_fn: str):
    logger.info(f"=== Seeding {len(PRODUCTS)} products ===")
    ok = 0
    for sku, name, procedencia, unit, price_usd, pen_note in PRODUCTS:
        payload = {
            "sku":          sku,
            "name":         name,
            "unit":         unit,
            "taxRate":      18,
            "isActive":     True,
            "enterpriseId": ENTERPRISE_ID,
            "metadata": {
                "procedencia": procedencia,
                "avg_price_note": (
                    pen_note if pen_note
                    else f"Precio promedio historico USD {price_usd}/{'kg' if unit == 'kg' else 'und'} — sujeto a variacion de mercado"
                ),
            },
        }
        # API requires basePrice — use 0.01 as placeholder for PEN-only products
        payload["basePrice"] = price_usd if price_usd is not None else 0.01

        try:
            resp = _invoke(product_fn, "POST", "/core/products", body=payload)
            pid = (resp.get("data") or resp).get("productId", "?")
            price_str = f"${price_usd}" if price_usd else "(sin precio USD)"
            logger.info(f"  ✅ [{sku}] {name[:45]} {price_str} → {pid}")
            ok += 1
        except RuntimeError as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower() or "409" in str(e):
                logger.info(f"  ⏭  [{sku}] {name[:45]} already exists — skipping")
                ok += 1
            else:
                logger.error(f"  ❌ [{sku}] {name[:45]}: {e}")

    logger.info(f"Products: {ok}/{len(PRODUCTS)} done\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Seed Lichan data into fourdist")
    parser.add_argument("--stage",  default="dev", choices=["dev", "qa", "prod"])
    parser.add_argument("--only",   choices=["customers", "products"], help="Seed only one entity type")
    args = parser.parse_args()

    suffixes = FUNCTION_SUFFIXES.get(args.stage)
    if not suffixes:
        logger.error(f"Unknown stage: {args.stage}")
        sys.exit(1)

    customer_fn = f"fourdist-{args.stage}-{suffixes['customer']}"
    product_fn  = f"fourdist-{args.stage}-{suffixes['product']}"

    logger.info(f"Stage: {args.stage}")
    logger.info(f"Customer fn: {customer_fn}")
    logger.info(f"Product fn:  {product_fn}\n")

    if args.only == "products":
        seed_products(product_fn)
    elif args.only == "customers":
        seed_customers(customer_fn)
    else:
        seed_customers(customer_fn)
        seed_products(product_fn)

    logger.info("Done.")


if __name__ == "__main__":
    main()
