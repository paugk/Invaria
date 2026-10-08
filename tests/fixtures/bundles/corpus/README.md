# Golden corpus del verificador (sintético)

Casos de verificación con su estado esperado, todos sobre el corpus sintético. `catalog.json` define cada caso así:
- **`bundle`**: uno de los bundles base de `bundles/`.
- **`overlay`** (opcional): los archivos de `overlays/<caso>/`, que se añaden o reemplazan.
- **`remove`** (opcional): rutas que se borran.
- **Verificación**: el nivel, si se usa `trust-store.json`, la política (`trust_at`, `min_signatures`) y el resultado esperado: estado, confianza, resultado financiero y número de observaciones renormalizadas en R2.

`tests/bundle/test_golden_corpus.py` exige lo siguiente:
- Cada caso da lo esperado, sin red.
- El catálogo cubre los cinco estados y usa todos los overlays.
- Ningún archivo contiene una clave privada.
- Los bundles base y los overlays sin firma son idénticos byte a byte a los que se generan de nuevo.

**Motor.** El corpus se grabó con `invaria-engine@0.1.0`, hoy retirado, y se conserva como evidencia histórica: los casos se reproducen con ese motor exacto (`engine_status: retired`) y las bases se reconstruyen con `replay(…, "invaria-engine@0.1.0")`, nunca con el motor actual. No se ha regenerado para 0.2.0, lo que exigiría claves efímeras nuevas.

**Firmas.** Se generaron con claves Ed25519 efímeras que se descartaron tras firmar. Aquí solo hay claves públicas (`trust-store.json`) y firmas (`overlays/signed*/signature.json`).

Si cambia el motor, un contrato o el formato, la comparación con un build nuevo falla a propósito. Regenerar es una decisión explícita, y crea claves nuevas:

    INVARIA_REGENERATE_GOLDEN_CORPUS=1 uv run --locked pytest tests/bundle/test_golden_corpus.py -k fresh_build

Después hay que revisar el diff y volver a ejecutar las pruebas sin la variable.

**Claves del trust store:**
- `invaria-golden-signer`: válida.
- `invaria-golden-rotated`: revocada (`superseded`) el 2026-10-04T12:00Z, después de firmar.
- `invaria-golden-compromised`: revocada por `key_compromise`.

`invaria-unknown-signer` no está en el trust store. Todas las firmas declaran `signed_at` 2026-10-03T10:00Z.
