# Achados do trivy config ignorados nos manifestos (make manifests), cada um com
# o motivo.
package trivy

import rego.v1

default ignore := false

# KSV-0109 le o PASSWORD :'var' do papeis.sql como senha gravada no ConfigMap;
# o valor e uma variavel do psql, lida do ambiente do container do banco (Secret
# execucao-postgres) por \getenv. So este ConfigMap, pelo nome inteiro (o do
# kustomize leva um hash de 10 caracteres): senha num outro continua reprovando,
# inclusive num de nome parecido. Dentro dele a excecao e larga, e o make
# manifests roda scripts/ci/confere-senhas-do-script.sh, que reprova PASSWORD com
# valor literal.
ignore if {
	input.ID == "KSV-0109"
	regex.match(`^ConfigMap 'execucao-postgres-init-[a-z0-9]{10}' `, input.Message)
}
