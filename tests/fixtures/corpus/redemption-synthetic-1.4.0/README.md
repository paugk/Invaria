# Corpus sintético de rescate, perfil 1.4.0 (histórico)

Generado por `../redemption-synthetic/build_corpus.py --profile=1.4.0`; no editar a mano.
Corpus histórico de rescate: perfil `fund-redemption-synthetic@1.4.0`, migrado con
`invaria-redemption-engine@0.5.0`, evaluado después por `0.6.0` y hoy
por `0.7.0` en modo compatible con su promesa (sin política declarada, el burn igual
no consulta la cuarentena), con los mismos resultados porque el corpus no contiene efectos
on-chain. El actual es `../redemption-synthetic-1.5.0`. Comparte con el corpus histórico
(`../redemption-synthetic`, perfil 1.3.0, motor retirado 0.4.0) los mismos bytes crudos,
observaciones, coberturas y resultados esperados escritos a mano; solo cambian el perfil
(`quarantine_scope` en el requisito de `stellar-testnet-frozen`), sus referencias y el
identificador del corpus. Todo es sintético.
