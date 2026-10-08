# Corpus sintético de rescate, perfil 1.5.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.5.0`; no editar a mano.
Corpus histórico de rescate desde la regla de ausencia con alcance: el actual es `../redemption-synthetic-1.6.0`.
Perfil `fund-redemption-synthetic@1.5.0`, evaluado por `invaria-redemption-engine@0.7.0`
y después por 0.8.0 a 0.10.0 con los mismos resultados: no declara
`absence_needs_chain_scope`, así que su certificado on-chain sin alcance sigue acreditando la
ausencia de liquidación (límite declarado). Comparte con los corpus históricos
(`../redemption-synthetic-1.4.0`, perfil 1.4.0; `../redemption-synthetic`, perfil 1.3.0) los
mismos bytes crudos, observaciones, coberturas y resultados esperados escritos a mano; solo
cambian el perfil (`quarantine_policy` declarada en el requisito de `stellar-testnet-frozen`),
sus referencias y el identificador del corpus. Los esperados no cambian porque el corpus no
contiene efectos on-chain ni certificados con registros en cuarentena, lo único que leen las
reglas de 0.5.0 a 0.7.0. Los casos de cuarentena por control en rescate se prueban en
`tests/engine/test_redemption_quarantine.py`. Todo es sintético.
