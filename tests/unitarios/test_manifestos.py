"""``k8s/``: o contrato dos manifestos como o cluster os recebe, nos tres overlays.

O ``make manifests`` confere o schema (kubeconform) e a configuracao (trivy), e
nao o que cada processo deve ter: um papel de banco trocado, o ``ENVIRONMENT``
em ``development``, uma sonda por ``true`` ou a NetworkPolicy que some passam por
ele. Estes testes leem o ``kubectl kustomize`` de cada overlay (``tests/manifestos.py``)
e prendem o contrato: Secret e chave por processo, papel do banco por processo,
Pod Security ``restricted``, token de ServiceAccount nao montado, entrada negada e
liberada so no previsto, sondas, teto do HPA e o prefixo da borda.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.manifestos import OVERLAYS, RAIZ, objeto, renderizar

if TYPE_CHECKING:
    from collections.abc import Iterator

NAMESPACE = "pytstop-execucao"
IMAGEM = "pytstop-execution-service"
POSTGRES = "postgres:16.15"
HOST_DO_BANCO = "execucao-postgres.pytstop-execucao.svc.cluster.local:5432/execucao"
# A DATABASE_URL se monta no pod, por expansao de variavel: o describe mostra o
# molde, nao a senha. Um molde so por papel, repetido em cada container.
MOLDE_DA_APLICACAO = (
    f"postgresql://execucao_app:$(POSTGRES_APP_PASSWORD)@{HOST_DO_BANCO}"
)
MOLDE_DO_DONO = f"postgresql://execucao:$(POSTGRES_OWNER_PASSWORD)@{HOST_DO_BANCO}"
TETO_DO_HPA = {"kind": 2, "kind-ci": 1, "k3s": 3}
TAG_DA_IMAGEM = {"kind": ":dev", "kind-ci": ":dev", "k3s": ""}
# (Deployment, container do processo)
_MENSAGERIA = [
    ("execution-service-relay", "relay"),
    ("execution-service-consumidor", "consumidor"),
]
_APLICACAO = [("execution-service-api", "api"), *_MENSAGERIA]
PROCESSOS_DA_MENSAGERIA = [pytest.param(*p, id=p[1]) for p in _MENSAGERIA]
PROCESSOS_DA_APLICACAO = [pytest.param(*p, id=p[1]) for p in _APLICACAO]


@pytest.fixture(params=OVERLAYS)
def overlay(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _pod(overlay: str, tipo: str, nome: str) -> dict[str, Any]:
    return dict(objeto(overlay, tipo, nome)["spec"]["template"]["spec"])


def _container(pod: dict[str, Any], nome: str) -> dict[str, Any]:
    [container] = [
        c
        for c in pod.get("initContainers", []) + pod["containers"]
        if c["name"] == nome
    ]
    return dict(container)


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {variavel["name"]: variavel for variavel in container.get("env", [])}


def _do_secret(nome: str, chave: str) -> dict[str, Any]:
    return {"secretKeyRef": {"name": nome, "key": chave}}


def _pods(overlay: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for documento in renderizar(overlay):
        if documento["kind"] in {"Deployment", "StatefulSet", "Job"}:
            yield documento["metadata"]["name"], documento["spec"]["template"]["spec"]


def _containers(pod: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from pod.get("initContainers", [])
    yield from pod["containers"]


# --- o que vale para todo pod --------------------------------------------------


def test_todo_pod_e_container_seguem_o_perfil_restrito(overlay: str) -> None:
    violacoes: list[str] = []
    pods = dict(_pods(overlay))
    assert set(pods) == {
        "execution-service-api",
        "execution-service-relay",
        "execution-service-consumidor",
        "execucao-postgres",
        "execucao-migracao",
    }
    for nome, pod in pods.items():
        seguranca = pod.get("securityContext", {})
        for ok, motivo in (
            (pod.get("automountServiceAccountToken") is False, "monta o token da SA"),
            (seguranca.get("runAsNonRoot") is True, "sem runAsNonRoot"),
            (seguranca.get("runAsUser", 0) > 0, "roda como root"),
            (
                seguranca.get("seccompProfile") == {"type": "RuntimeDefault"},
                "seccomp fora de RuntimeDefault",
            ),
            (not pod.get("hostNetwork"), "usa a rede do no"),
        ):
            if not ok:
                violacoes.append(f"{nome}: {motivo}")
        for container in _containers(pod):
            contexto = container.get("securityContext", {})
            recursos = container.get("resources", {})
            for ok, motivo in (
                (
                    contexto.get("allowPrivilegeEscalation") is False,
                    "escala privilegio",
                ),
                (contexto.get("readOnlyRootFilesystem") is True, "raiz gravavel"),
                (contexto.get("capabilities") == {"drop": ["ALL"]}, "com capability"),
                (not contexto.get("privileged"), "privilegiado"),
                (
                    {"cpu", "memory"} <= set(recursos.get("requests", {}))
                    and {"cpu", "memory"} <= set(recursos.get("limits", {})),
                    "sem requests e limits de cpu e memoria",
                ),
            ):
                if not ok:
                    violacoes.append(f"{nome}/{container['name']}: {motivo}")
    assert violacoes == []


def test_namespace_com_pod_security_restricted_em_enforce_warn_e_audit(
    overlay: str,
) -> None:
    rotulos = objeto(overlay, "Namespace", NAMESPACE)["metadata"]["labels"]

    for modo in ("enforce", "warn", "audit"):
        assert rotulos[f"pod-security.kubernetes.io/{modo}"] == "restricted"
        assert rotulos[f"pod-security.kubernetes.io/{modo}-version"] == "latest"


def test_nenhum_secret_nos_manifestos_e_todo_objeto_no_namespace_do_servico(
    overlay: str,
) -> None:
    documentos = renderizar(overlay)

    assert "Secret" not in {documento["kind"] for documento in documentos}
    fora = [
        f"{documento['kind']}/{documento['metadata']['name']}"
        for documento in documentos
        if documento["kind"] != "Namespace"
        and documento["metadata"].get("namespace") != NAMESPACE
    ]
    assert fora == []


def test_imagem_do_servico_e_a_do_postgres_tem_uma_versao_so(overlay: str) -> None:
    imagens = {
        nome: {c["image"] for c in _containers(pod)} for nome, pod in _pods(overlay)
    }

    for nome in ("execution-service-api", "execution-service-relay"):
        assert imagens[nome] == {IMAGEM + TAG_DA_IMAGEM[overlay]}
    assert imagens["execution-service-consumidor"] == {IMAGEM + TAG_DA_IMAGEM[overlay]}
    # O Job migra com a imagem do servico e espera o banco com a do PostgreSQL.
    assert imagens["execucao-migracao"] == {IMAGEM + TAG_DA_IMAGEM[overlay], POSTGRES}
    assert imagens["execucao-postgres"] == {
        POSTGRES,
        "prometheuscommunity/postgres-exporter:v0.20.1",
    }


def test_a_versao_do_postgres_vem_so_da_kustomization() -> None:
    base = RAIZ / "k8s" / "base"
    literais = [
        arquivo.name
        for arquivo in sorted(base.glob("*.yaml"))
        if "postgres:16" in arquivo.read_text() and arquivo.name != "kustomization.yaml"
    ]

    assert literais == []
    assert POSTGRES.split(":")[1] in (base / "kustomization.yaml").read_text()


# --- papel do banco e Secret por processo --------------------------------------


@pytest.mark.parametrize(("deployment", "container"), PROCESSOS_DA_APLICACAO)
def test_api_relay_e_consumidor_so_usam_o_papel_da_aplicacao(
    overlay: str, deployment: str, container: str
) -> None:
    pod = _pod(overlay, "Deployment", deployment)

    # O initContainer que espera a migracao e o processo, com o mesmo molde.
    for nome in ("aguarda-migracao", container):
        ambiente = _env(_container(pod, nome))
        assert ambiente["DATABASE_URL"]["value"] == MOLDE_DA_APLICACAO
        assert ambiente["POSTGRES_APP_PASSWORD"]["valueFrom"] == _do_secret(
            "execucao-postgres", "POSTGRES_APP_PASSWORD"
        )
        assert set(ambiente) <= {
            "DATABASE_URL",
            "POSTGRES_APP_PASSWORD",
            "RABBITMQ_URL",
        }


def test_so_o_job_usa_o_dono_do_banco_para_migrar(overlay: str) -> None:
    pod = _pod(overlay, "Job", "execucao-migracao")
    migracao = _container(pod, "migracao")
    ambiente = _env(migracao)

    assert ambiente["DATABASE_URL"]["value"] == MOLDE_DO_DONO
    assert ambiente["POSTGRES_OWNER_PASSWORD"]["valueFrom"] == _do_secret(
        "execucao-postgres", "POSTGRES_OWNER_PASSWORD"
    )
    assert set(ambiente) == {"DATABASE_URL", "POSTGRES_OWNER_PASSWORD"}
    # Migra, tira da aplicacao a escrita na alembic_version e semeia, nessa ordem.
    [_, _, comando] = migracao["command"]
    assert comando == (
        "alembic upgrade head && python -m src.banco restringir "
        "&& python -m src.estoque.infraestrutura.seed"
    )
    for deployment, container in _APLICACAO:
        env = _env(_container(_pod(overlay, "Deployment", deployment), container))
        assert "POSTGRES_OWNER_PASSWORD" not in env
        assert "POSTGRES_PASSWORD" not in env


def test_exporter_so_usa_o_papel_de_monitoramento(overlay: str) -> None:
    pod = _pod(overlay, "StatefulSet", "execucao-postgres")
    exporter = _container(pod, "exporter")
    ambiente = _env(exporter)

    assert ambiente["DATA_SOURCE_USER"]["value"] == "execucao_exporter"
    assert ambiente["DATA_SOURCE_PASS"]["valueFrom"] == _do_secret(
        "execucao-postgres", "POSTGRES_EXPORTER_PASSWORD"
    )
    assert exporter["securityContext"]["runAsUser"] == 65534
    # Container que nao grava nada fica sem o /tmp.
    assert "volumeMounts" not in exporter


def test_postgres_sem_trust_e_com_uma_senha_por_papel(overlay: str) -> None:
    pod = _pod(overlay, "StatefulSet", "execucao-postgres")
    ambiente = _env(_container(pod, "postgres"))

    for chave in (
        "POSTGRES_PASSWORD",
        "POSTGRES_OWNER_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_EXPORTER_PASSWORD",
    ):
        assert ambiente[chave]["valueFrom"] == _do_secret("execucao-postgres", chave)
    argumentos = ambiente["POSTGRES_INITDB_ARGS"]["value"].split()
    assert argumentos == [
        "--auth-host=scram-sha-256",
        "--auth-local=scram-sha-256",
    ]
    assert ambiente["POSTGRES_DB"]["value"] == "execucao"


def test_so_relay_e_consumidor_leem_a_url_do_broker_do_secret_da_plataforma(
    overlay: str,
) -> None:
    for deployment, container in _MENSAGERIA:
        ambiente = _env(_container(_pod(overlay, "Deployment", deployment), container))
        assert ambiente["RABBITMQ_URL"]["valueFrom"] == _do_secret(
            "rabbitmq", "RABBITMQ_URL"
        )
    api = _env(_container(_pod(overlay, "Deployment", "execution-service-api"), "api"))
    assert "RABBITMQ_URL" not in api


def test_banco_com_volume_do_overlay(overlay: str) -> None:
    statefulset = objeto(overlay, "StatefulSet", "execucao-postgres")
    [volume] = statefulset["spec"]["volumeClaimTemplates"]

    assert volume["spec"]["resources"]["requests"]["storage"] == (
        "5Gi" if overlay == "k3s" else "1Gi"
    )
    assert statefulset["spec"]["replicas"] == 1


# --- configuracao ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tipo", "workload", "container"),
    [
        pytest.param("Deployment", "execution-service-api", "api", id="api"),
        pytest.param("Deployment", "execution-service-relay", "relay", id="relay"),
        pytest.param(
            "Deployment", "execution-service-consumidor", "consumidor", id="consumidor"
        ),
        pytest.param("Job", "execucao-migracao", "migracao", id="migracao"),
    ],
)
def test_todo_processo_do_servico_le_o_configmap_gerado(
    overlay: str, tipo: str, workload: str, container: str
) -> None:
    configmap = objeto(overlay, "ConfigMap", "execution-service")["metadata"]["name"]
    processo = _container(_pod(overlay, tipo, workload), container)

    assert processo["envFrom"] == [{"configMapRef": {"name": configmap}}]


def test_o_configmap_liga_a_guarda_de_producao_e_o_prefixo_da_borda(
    overlay: str,
) -> None:
    dados = objeto(overlay, "ConfigMap", "execution-service")["data"]
    prefixos = {
        "/" + caminho["path"].split("/")[1]
        for documento in renderizar(overlay)
        if documento["kind"] == "Ingress"
        for regra in documento["spec"]["rules"]
        for caminho in regra["http"]["paths"]
    }

    # Sem a guarda ligada, as senhas de demonstracao do compose valeriam no cluster.
    assert dados["ENVIRONMENT"] == "production"
    assert dados["RUN_MIGRATIONS_ON_STARTUP"] == dados["RUN_SEED_ON_STARTUP"] == "false"
    assert prefixos == {dados["ROOT_PATH"]} == {"/execucao"}
    assert dados["METRICS_PORT"] == "9100"
    assert "TRUSTED_PROXIES" not in dados  # o entrypoint usa --no-proxy-headers


def test_toda_variavel_do_configmap_e_lida_pelo_codigo(overlay: str) -> None:
    codigo = "\n".join(p.read_text() for p in (RAIZ / "src").rglob("*.py"))
    entrypoint = (RAIZ / "entrypoint.sh").read_text()
    dados = objeto(overlay, "ConfigMap", "execution-service")["data"]

    sem_leitor = [
        variavel
        for variavel in dados
        if f'"{variavel}"' not in codigo and "${" + variavel not in entrypoint
    ]

    assert sem_leitor == []


# --- sondas, replicas e encerramento ---------------------------------------------


def test_api_tem_startup_liveness_e_readiness_http(overlay: str) -> None:
    api = _container(_pod(overlay, "Deployment", "execution-service-api"), "api")

    assert api["startupProbe"] == {
        "httpGet": {"path": "/api/v1/saude", "port": "http"},
        "periodSeconds": 2,
        "timeoutSeconds": 3,
        "failureThreshold": 30,
    }
    assert api["livenessProbe"] == {
        "httpGet": {"path": "/api/v1/saude", "port": "http"},
        "periodSeconds": 10,
        "timeoutSeconds": 3,
        "failureThreshold": 3,
    }
    # A readiness olha o banco; a liveness, nao.
    assert api["readinessProbe"] == {
        "httpGet": {"path": "/api/v1/saude/pronto", "port": "http"},
        "periodSeconds": 5,
        "timeoutSeconds": 3,
    }


@pytest.mark.parametrize(("deployment", "container"), PROCESSOS_DA_MENSAGERIA)
def test_relay_e_consumidor_sondam_o_heartbeat_e_o_pronto_por_arquivo(
    overlay: str, deployment: str, container: str
) -> None:
    processo = _container(_pod(overlay, "Deployment", deployment), container)
    liveness = processo["livenessProbe"]["exec"]["command"]

    # Heartbeat com mais de 90 s: o laco parou.
    assert liveness[:2] == ["python", "-c"]
    assert f"/tmp/{container}-heartbeat" in liveness[2]  # noqa: S108 - o emptyDir do pod
    assert liveness[2].endswith(">90)")
    assert processo["livenessProbe"]["periodSeconds"] == 10
    assert processo["livenessProbe"]["timeoutSeconds"] == 5
    # A startup e a liveness: a existencia do arquivo bastaria com o arquivo do
    # container morto no emptyDir. Sem o pronto, o broker fora no boot nao reinicia.
    assert processo["startupProbe"] == {
        "exec": {"command": liveness},
        "periodSeconds": 2,
        "timeoutSeconds": 5,
        "failureThreshold": 30,
    }
    assert processo["readinessProbe"] == {
        "exec": {"command": ["test", "-f", f"/tmp/{container}-pronto"]},  # noqa: S108
        "periodSeconds": 10,
    }


def test_replicas_hpa_e_encerramento_gracioso(overlay: str) -> None:
    api = objeto(overlay, "Deployment", "execution-service-api")
    hpa = objeto(overlay, "HorizontalPodAutoscaler", "execution-service-api")

    # Quem decide as replicas da API e o HPA, com o teto do overlay.
    assert "replicas" not in api["spec"]
    assert hpa["spec"]["scaleTargetRef"]["name"] == "execution-service-api"
    assert hpa["spec"]["minReplicas"] == 1
    assert hpa["spec"]["maxReplicas"] == TETO_DO_HPA[overlay]
    assert hpa["spec"]["metrics"][0]["resource"]["target"] == {
        "type": "Utilization",
        "averageUtilization": 70,
    }
    # Relay e consumidor: uma replica, e 45 s para terminar o lote em curso.
    for deployment in ("execution-service-relay", "execution-service-consumidor"):
        documento = objeto(overlay, "Deployment", deployment)
        assert documento["spec"]["replicas"] == 1
        assert (
            documento["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 45
        )


def test_job_de_migracao_e_o_de_inicializacao_que_o_script_do_platform_apaga(
    overlay: str,
) -> None:
    job = objeto(overlay, "Job", "execucao-migracao")
    pod = job["spec"]["template"]["spec"]

    assert job["metadata"]["labels"]["app.kubernetes.io/component"] == "inicializacao"
    assert job["spec"]["backoffLimit"] == 4
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert pod["restartPolicy"] == "OnFailure"
    [espera] = pod["initContainers"]
    assert espera["command"][-1].startswith("until pg_isready -h execucao-postgres")


def test_initcontainer_aguarda_a_migracao_pelo_modulo_do_servico(overlay: str) -> None:
    for deployment, _ in _APLICACAO:
        espera = _container(_pod(overlay, "Deployment", deployment), "aguarda-migracao")

        assert espera["command"] == ["python", "-m", "src.banco", "aguardar"]


# --- rede e observabilidade ------------------------------------------------------

_PLATAFORMA = {"kubernetes.io/metadata.name": "pytstop-plataforma"}


def test_entrada_negada_a_todo_pod_e_liberada_so_no_previsto(overlay: str) -> None:
    politicas = {
        documento["metadata"]["name"]: documento["spec"]
        for documento in renderizar(overlay)
        if documento["kind"] == "NetworkPolicy"
    }

    assert politicas["nega-entrada"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert set(politicas) == {
        "nega-entrada",
        "execution-service-api",
        "execution-service-metricas",
        "execucao-postgres",
    }
    assert politicas["execution-service-api"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": _PLATAFORMA},
                    "podSelector": {
                        "matchExpressions": [
                            {
                                "key": "app",
                                "operator": "In",
                                "values": ["kong", "prometheus"],
                            }
                        ]
                    },
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8000}],
        }
    ]
    metricas = politicas["execution-service-metricas"]
    assert metricas["podSelector"]["matchExpressions"][0]["values"] == [
        "relay",
        "consumidor",
    ]
    [prometheus] = metricas["ingress"]
    assert prometheus["ports"] == [{"protocol": "TCP", "port": 9100}]
    assert prometheus["from"][0]["podSelector"] == {
        "matchLabels": {"app": "prometheus"}
    }
    # O banco: o proprio namespace (podSelector vazio) e o Prometheus no exporter.
    [do_namespace, do_prometheus] = politicas["execucao-postgres"]["ingress"]
    assert do_namespace == {
        "from": [{"podSelector": {}}],
        "ports": [{"protocol": "TCP", "port": 5432}],
    }
    assert do_prometheus["ports"] == [{"protocol": "TCP", "port": 9187}]
    assert all(
        politica.get("policyTypes") == ["Ingress"] for politica in politicas.values()
    )


def test_politicas_e_services_selecionam_pods_que_existem(overlay: str) -> None:
    rotulos = {
        nome: documento["spec"]["template"]["metadata"]["labels"]
        for documento in renderizar(overlay)
        if documento["kind"] in {"Deployment", "StatefulSet"}
        for nome in [documento["metadata"]["name"]]
    }
    selecoes = {
        documento["metadata"]["name"]: documento["spec"]["selector"]
        for documento in renderizar(overlay)
        if documento["kind"] == "Service"
    }

    # Todo Service da API (interno e da borda) acha os pods da API, e o do banco,
    # o StatefulSet.
    for nome, seletor in selecoes.items():
        alvo = (
            "execucao-postgres"
            if nome == "execucao-postgres"
            else "execution-service-api"
        )
        assert seletor.items() <= rotulos[alvo].items(), nome
    assert rotulos["execution-service-relay"]["processo"] == "relay"
    assert rotulos["execution-service-consumidor"]["processo"] == "consumidor"
    assert rotulos["execucao-postgres"]["processo"] == "banco"


def test_prometheus_raspa_a_porta_que_o_container_declara(overlay: str) -> None:
    esperado = {
        "execution-service-api": ("8000", "api", "http"),
        "execution-service-relay": ("9100", "relay", "metrics"),
        "execution-service-consumidor": ("9100", "consumidor", "metrics"),
        "execucao-postgres": ("9187", "exporter", "metrics"),
    }
    for nome, (porta, container, porta_nomeada) in esperado.items():
        documento = objeto(
            overlay,
            "StatefulSet" if nome == "execucao-postgres" else "Deployment",
            nome,
        )
        anotacoes = documento["spec"]["template"]["metadata"]["annotations"]
        declaradas = _container(documento["spec"]["template"]["spec"], container)[
            "ports"
        ]

        assert anotacoes == {
            "prometheus.io/scrape": "true",
            "prometheus.io/port": porta,
            "prometheus.io/path": "/metrics",
        }
        assert [(p["name"], str(p["containerPort"])) for p in declaradas] == [
            (porta_nomeada, porta)
        ]
