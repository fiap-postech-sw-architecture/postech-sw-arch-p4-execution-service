# Achados do trivy config ignorados nos manifestos (make manifests), cada um com
# o motivo.
package trivy

import rego.v1

default ignore := false

# KSV-0109 le o PASSWORD :'var' do papeis.sql como senha gravada no ConfigMap;
# o valor e uma variavel do psql, lida do ambiente do container do banco (Secret
# execucao-postgres) por \getenv. So este ConfigMap: senha num outro continua
# reprovando. Dentro dele a excecao e larga, e o make manifests roda
# scripts/ci/confere-senhas-do-script.sh, que reprova PASSWORD com valor literal.
ignore if {
	input.ID == "KSV-0109"
	startswith(input.Message, "ConfigMap 'execucao-postgres-init-")
}
