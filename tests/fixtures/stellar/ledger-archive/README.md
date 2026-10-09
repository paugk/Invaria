# Evidencia de archivos de historia (verificación de ledgers)

Datos **reales** de Stellar testnet, sin relación con clientes. No hay claves. La guía reproducible está en [`LEDGER_VERIFICATION.md`](../../../../LEDGER_VERIFICATION.md).

`checkpoint-5027711/` contiene el checkpoint de las transacciones T1, T2 y T3 de la emisión propia DEMOA (ledgers 5027648–5027711):
- `archive/`: los bytes de `history-`, `ledger-`, `transactions-` y `results-004cb77f`, tal como se descargaron del archivo `core_testnet_001` el 2026-10-08, con `provenance.json` (URL, sha256, tamaño y hora). Los hashes de las cabeceras protegen su contenido XDR, no los bytes `.gz`.
- `anchor-run/`: la ejecución de `stellar-core verify-checkpoints --from-ledger 5027711` (2026-10-08, 23:00-23:05Z), con la imagen fijada por digest. Contiene:
  - la configuración (validadores `sdftest1` a `sdftest3` de SDF), los anclajes y el log filtrado (sin líneas de depuración ni de conexión entre pares, sin IP);
  - `anchor-run.json`, que liga todo por sha256 con la versión y el digest de la imagen.
- `replay/`: la ejecución de `catchup 5027711/64` (2026-10-08, 23:53-23:54Z, 75 s). Contiene:
  - el flujo completo de `LedgerCloseMeta` de los 64 ledgers (`meta.xdr.gz`) y el estado del historial del checkpoint inicial 5027647, con las referencias de los 36 buckets;
  - la configuración, el script de descarga contabilizado, el registro de descargas, el de recursos y el log filtrado;
  - `replay-run.json`, con los bytes que escribió la ejecución: armado con sus datos al terminar (fin a las 23:54:36Z, archivo a las 23:58Z). Su sha256, `b7459be8c89d05d360e990cdf81bec4c7a5c7930deca6aa8f8d55c48deb9cdc2`, se calculó por primera vez el 2026-10-09, no durante la ejecución;
  - `replay-run.revision-1.json` (sha256 `89b1b95f79f78710cd4ed89228e2947e6b4dea766b6bb0b427348110cab66ddd`), una revisión aparte del 2026-10-09. Nombra el original por sha256, da el motivo y declara `download.log` y `resources.log` como `added_after_run`, con `hashes_contemporaneous: false`: el ejecutor no los hasheó, y sus bytes se compararon ese día con sus copias de trabajo.

  **Los buckets no se conservan**: el replay completo no se puede repetir offline con estos archivos. El meta no está comprometido por ninguna cabecera: se deriva de un replay verificado.

Ninguno de los dos registros se reconstruyó ni se editó después.

Comparar el registro con un hash esperado acredita la correspondencia con el registro esperado. No acredita por sí solo que la ejecución ocurriera, ni que quien la documentó lo hiciera con verdad. Los sha256 publicados aquí y en las pruebas (`REPLAY_RECORD_SHA256`, `REPLAY_REVISION_SHA256`) se distribuyen por el mismo canal que las fixtures, así que no son un canal independiente.

El verificador del replay rechaza cualquier archivo de `replay/` no declarado en el registro o en su revisión. Rehacer estas fixtures es una decisión explícita: las pruebas de `tests/stellar/test_ledger_proof.py` fijan sus hashes.
