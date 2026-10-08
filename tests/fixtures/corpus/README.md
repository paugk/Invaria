# Corpus

| Corpus | Perfil | Motor | Estado |
|---|---|---|---|
| `subscription-synthetic-1.2.0/` | `fund-subscription-synthetic@1.2.0` (1.1.0 más `completeness_needs_coverage`) | `invaria-engine@0.10.0` | Actual. Datos iguales a 1.1.0; esperados iguales. |
| `subscription-synthetic-1.1.0/` | `fund-subscription-synthetic@1.1.0` (1.0.0 más `absence_needs_chain_scope`) | `invaria-engine@0.10.0` sin las reglas de la segunda parte | **Histórico** desde la regla de completitud. Crudos, mappings, observaciones y cobertura iguales a 1.0.0; esperados escritos a mano desde la regla (K2, K2-historical y DUPLICATE-DELIVERY pasan a UNKNOWN; K3 sigue BREAK). |
| `subscription-synthetic/` | `fund-subscription-synthetic@1.0.0` | `invaria-engine@0.9.0` (sin la regla de ausencia con alcance, que el perfil no declara) | **Histórico** desde esa regla; sigue siendo la base del golden K3, de los bundles y del verificador y de las pruebas de persistencia. Su certificado on-chain no declara alcance: acredita la ausencia solo bajo este perfil (límite declarado). |
| `subscription-testnet-1.6.0/` | `fund-subscription-testnet@1.6.0` (1.5.0 más `completeness_needs_coverage`) | `invaria-engine@0.10.0` | Actual (demo). Datos y esperados iguales a 1.5.0: el certificado SAC del adaptador es coherente y de la representación DEMOA. |
| `subscription-testnet-1.5.0/` | `fund-subscription-testnet@1.5.0` (1.4.0 más `absence_needs_chain_scope`) | `invaria-engine@0.10.0` sin la segunda parte | **Histórico** desde la regla de completitud. Crudos, identidades, plan, enlaces y esperados iguales a 1.4.0: los certificados del adaptador declaran un alcance suficiente. |
| `subscription-testnet-1.4.0/` | `fund-subscription-testnet@1.4.0` (1.3.0 más los mappings admitidos para la fuente on-chain) | `invaria-engine@0.9.0` | **Histórico** desde la regla de ausencia con alcance; sigue dando 4/4. Crudos, identidades, plan, enlaces y esperados iguales a 1.3.0. |
| `subscription-testnet-1.3.0/` | `fund-subscription-testnet@1.3.0` (declara `muxed_account_link` y el mapping 1.1.0) | `invaria-engine@0.8.0` | **Histórico** desde la admisión explícita de mappings; sigue dando 4/4 (DEMOA solo usa el mapping que admite). |
| `subscription-testnet-1.2.0/` | `fund-subscription-testnet@1.2.0` (declara `quarantine_policy`) | Ninguno en evaluación nueva: 0.8.0 la rechaza por procedencia (antes 0.6.0 y 0.7.0) | **Histórico** desde las cuentas muxed. Rechazado por procedencia. |
| `subscription-testnet-1.1.0/` | `fund-subscription-testnet@1.1.0` | Ninguno en evaluación nueva: 0.8.0 la rechaza por procedencia (antes 0.2.0 a 0.7.0) | **Histórico** desde la política de cuarentena declarada. Rechazado por procedencia. |
| `subscription-testnet/` | `fund-subscription-testnet@1.0.0` | `invaria-engine@0.1.0` (retirado) | **Histórico**. Se conserva para reproducir. |
| `redemption-synthetic-1.7.0/` | `fund-redemption-synthetic@1.7.0` (1.6.0 más `completeness_needs_coverage`) | `invaria-redemption-engine@0.11.0` | Actual. Lo genera `build_corpus.py --profile=1.7.0`; `_v17` aplica M3 a los esperados de 1.6.0 (29 burns iguales pasan a UNKNOWN; 6 MATCH pasan a UNKNOWN; los BREAK se conservan). |
| `redemption-synthetic-1.6.0/` | `fund-redemption-synthetic@1.6.0` (1.5.0 más `absence_needs_chain_scope`) | `invaria-redemption-engine@0.11.0` sin la segunda parte | **Histórico** desde la regla de completitud. Lo genera `build_corpus.py --profile=1.6.0`; cuatro esperados escritos a mano (`V16_EXPECTED`). |
| `redemption-synthetic-1.5.0/` | `fund-redemption-synthetic@1.5.0` (declara `quarantine_policy`) | `invaria-redemption-engine@0.10.0` sin la regla de ausencia con alcance (antes 0.7.0 a 0.9.0) | **Histórico** desde esa regla. Lo genera `build_corpus.py --profile=1.5.0`. |
| `redemption-synthetic-1.4.0/` | `fund-redemption-synthetic@1.4.0` | `invaria-redemption-engine@0.8.0` en modo compatible (antes 0.5.0 a 0.7.0) | **Histórico** desde la política de cuarentena declarada. Lo genera `build_corpus.py --profile=1.4.0`. |
| `redemption-synthetic/` | `fund-redemption-synthetic@1.3.0` | `invaria-redemption-engine@0.4.0` (retirado) | **Histórico**. Se conserva para reproducir; aquí vive el generador. |

Cada README de corpus explica su migración.

**Desde el 2026-10-08:** los corpus de suscripción testnet 1.0.0, 1.1.0 y 1.2.0 admiten `stellar-classic-payment@1.0.0`. Con la evidencia que produce hoy el adaptador (1.1.0), una evaluación nueva se rechaza con diagnóstico; no se añade un modo de ingesta 1.0.0.

**Desde el 2026-10-08:** los perfiles que declaran `absence_needs_chain_scope` (suscripción sintética 1.1.0, testnet 1.5.0, rescate 1.6.0) no aceptan que un certificado on-chain sin alcance declarado suficiente acredite una ausencia. Los anteriores conservan su promesa. Con ellos, un certificado sin alcance sigue acreditándola: es su límite declarado, que solo se cierra con el perfil nuevo.

**Regla de completitud (2026-10-08):** los perfiles que declaran `completeness_needs_coverage` (suscripción sintética 1.2.0, testnet 1.6.0, rescate 1.7.0) exigen, para afirmar que lo observado es completo, cobertura con un alcance coherente con su ruta y su rol de cada representación del instrumento. En rescate eso incluye un burn igual o corto. Los perfiles anteriores conservan sus reglas y limitaciones, y reproducir sus resultados no los actualiza.
