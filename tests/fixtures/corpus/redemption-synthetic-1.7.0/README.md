# Corpus sintético de rescate, perfil 1.7.0 (actual)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.7.0`; no editar a mano.
Es el corpus actual de rescate: perfil `fund-redemption-synthetic@1.7.0`, evaluado por
`invaria-redemption-engine@0.11.0` (regla de completitud). Comparte con los
corpus históricos los mismos bytes crudos, observaciones y coberturas. El perfil añade
`completeness_needs_coverage` al requisito de `stellar-testnet-frozen`: afirmar que los burns
observados son todos los burns exige cobertura con un alcance coherente de la representación de
los burns, de la aceptación al corte.

Los certificados on-chain de este corpus no declaran alcance, así que todo burn igual pasa a
UNKNOWN (`INSUFFICIENT_COVERAGE`). Lo aplica `_v17` a los esperados de 1.6.0, desde la regla y
no desde el motor. Seis MATCH pasan a UNKNOWN (RD-PAID, RD-PAID-AT-DUE,
RD-POSITION-RECONSTRUCTED, RD-CANCEL-RETRACTED-AFTER, RD-CORRECTION-KNOWN-LATE, RD-DUPLICATE) y
todos los BREAK se conservan. El corpus no tiene burns cortos
ni en exceso: esos casos, y el alcance suficiente, se prueban en
`tests/engine/test_scoped_absence.py`. Todo es sintético.
