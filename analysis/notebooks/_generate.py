"""Gera 01_baseline.ipynb programaticamente.

Cada célula tem markdown explicativo ANTES e nota interpretativa DEPOIS.
Foco: agregação por (setup, workload, clients) — escala pra 720+ runs.

Uso:
    cd analysis/notebooks
    python _generate.py
"""
import json
import uuid
from pathlib import Path

NB_PATH = Path(__file__).parent / "01_baseline.ipynb"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def md(text: str) -> dict:
    return {
        "id": _new_id(),
        "cell_type": "markdown",
        "metadata": {},
        "source": text.splitlines(keepends=True),
    }


def code(src: str) -> dict:
    return {
        "id": _new_id(),
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": src.splitlines(keepends=True),
    }


CELLS = [
    md("""# Análise — TCC Connection Poolers

**Pergunta de pesquisa**: Em qual ponto o overhead de um connection pooler é compensado pela redução de contenção no PostgreSQL?

Este notebook agrega dados gerados pelo orquestrador (`data/{exp_id}/`) e produz visualizações comparativas. Cada gráfico tem uma seção explicativa antes (o que mostra, como ler) e uma observação interpretativa depois (o que esperar / o que de fato apareceu).

**Premissa metodológica**: as comparações justas são entre runs com mesmo `workload`, `mode` e `clients` — variando apenas `setup` (pooler) e `pool_size`.
"""),

    code("""import sys
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

try:
    import seaborn as sns
    sns.set_theme(style="whitegrid")
    HAS_SNS = True
except ImportError:
    HAS_SNS = False

ROOT = Path.cwd().resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.loader import (
    load_experiments, tps_dataframe,
    master_activity, master_locks,
    master_statements_deltas, master_io_deltas,
    wait_event_distribution,
)

pd.set_option("display.max_columns", 50)
pd.set_option("display.width", 200)

DATA_DIR = (Path.cwd() / ".." / ".." / "data").resolve()
print(f"DATA_DIR = {DATA_DIR}  exists={DATA_DIR.exists()}")
"""),

    md("""## 1. Carregar dados

Lê `data/{exp_id}/` em paralelo. Cada experimento vira um dict com 4 DataFrames (activity, locks, statements, io) + config.

Runs com `status=expected_failure` ou `failed` antes do collector capturar config têm `label={}` e dados zerados — os helpers de agregação pulam esses silenciosamente.
"""),

    code("""experiments = {}
if DATA_DIR.exists():
    experiments = load_experiments(DATA_DIR)
print(f"Experimentos carregados: {len(experiments)}")
"""),

    md("""## 2. Sumário da bateria

Visão tabular: quantos runs por (setup, clients), quantos têm dados utilizáveis (tps válido).
"""),

    code("""tps_df = tps_dataframe(experiments, DATA_DIR) if experiments else pd.DataFrame()
print(f"Runs com TPS válido: {len(tps_df)} de {len(experiments)}")

if not tps_df.empty:
    sumario = (
        tps_df.assign(has_tps=tps_df["tps"].notna())
              .groupby(["setup", "workload", "clients"], dropna=False)
              .agg(n_runs=("rep", "count"), n_com_tps=("has_tps", "sum"),
                   tps_mediano=("tps", "median"), lat_p50_ms=("latency_avg_ms", "median"))
              .round(1)
    )
    display(sumario)
else:
    print("Sem dados de TPS pra sumarizar.")
"""),

    md("""**Como ler**: `n_runs` = quantas reps daquela combinação rodaram; `n_com_tps` = quantas geraram log válido (rest = failed/expected_failure). `tps_mediano` é uma estatística robusta (vs média que sofre com outliers).
"""),

    md("""## 3. Throughput (TPS) vs concorrência

**O que mostra**: TPS mediano em função do número de clientes, uma linha por setup. Eixo X em log porque concorrência varia ordens de magnitude.

**Como ler**:
- Subindo a linha = pooler aguenta mais carga.
- Linha CAIR ou ZERAR depois de um ponto = colapso (típico do `none` excedendo `max_connections=415`).
- PgBouncer `pool=8` deveria saturar em ~8 backends — TPS plateau após esse ponto.
- Pgpool `num_init_children=32` deveria BLOQUEAR (sem TPS) acima de 32 clientes.
"""),

    code("""def plot_tps_curve(tps_df, workload):
    if tps_df.empty:
        print(f"sem dados pra workload={workload}")
        return
    df = tps_df[tps_df["workload"] == workload].dropna(subset=["tps", "clients"])
    if df.empty:
        print(f"sem dados pra workload={workload}")
        return
    agg = df.groupby(["setup", "clients"])["tps"].median().reset_index()

    fig, ax = plt.subplots(figsize=(8, 5))
    for setup in sorted(agg["setup"].unique()):
        sub = agg[agg["setup"] == setup].sort_values("clients")
        ax.plot(sub["clients"], sub["tps"], marker="o", label=setup, linewidth=2)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Clientes concorrentes (log)")
    ax.set_ylabel("TPS mediano (log)")
    ax.set_title(f"Throughput vs concorrência — workload={workload}")
    ax.legend(title="Setup")
    ax.grid(True, which="both", alpha=0.3)
    plt.tight_layout()
    plt.show()

for wl in ("read", "write", "mixed"):
    plot_tps_curve(tps_df, wl)
"""),

    md("""**O que observar**:
- Em **read**: PgBouncer-tx deve manter TPS alto até concorrências grandes (queue interna + multiplexação). `none` deve cair quando excede `max_connections`.
- Em **write**: contenção de WAL/locks limita TPS a centenas. Pooler reduz contenção (visto na seção de wait events) mas TPS absoluto continua baixo.
- **Pgpool**: pontos esparsos — só onde clients ≤ num_init_children.
"""),

    md("""## 4. Latência média vs concorrência

**O que mostra**: latência média (ms) por cliente em função da concorrência, por setup. Mesmas regras de leitura do TPS.

**Esperado**: latência cresce com concorrência (mais espera). Pooler em transaction-mode pode adicionar handshake-overhead em concorrência baixa, mas em concorrência alta DEVE ter latência menor que `none` (fila ordenada > thrashing).
"""),

    code("""def plot_latency_curve(tps_df, workload):
    if tps_df.empty:
        return
    df = tps_df[tps_df["workload"] == workload].dropna(subset=["latency_avg_ms", "clients"])
    if df.empty:
        return
    agg = df.groupby(["setup", "clients"])["latency_avg_ms"].median().reset_index()
    fig, ax = plt.subplots(figsize=(8, 5))
    for setup in sorted(agg["setup"].unique()):
        sub = agg[agg["setup"] == setup].sort_values("clients")
        ax.plot(sub["clients"], sub["latency_avg_ms"], marker="o", label=setup, linewidth=2)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Clientes concorrentes (log)")
    ax.set_ylabel("Latência média (ms, log)")
    ax.set_title(f"Latência média vs concorrência — workload={workload}")
    ax.legend(title="Setup")
    plt.tight_layout()
    plt.show()

for wl in ("read", "write", "mixed"):
    plot_latency_curve(tps_df, wl)
"""),

    md("""**O que observar**:
- Em **read** baixo (c=1-10): pooler ADICIONA latência (overhead do hop extra). Esperado.
- Em **read** alto (c=100+): pooler REDUZ latência se sem-pooler está em colapso.
- O **break-even** é o ponto onde as curvas se cruzam — peça central da pergunta de pesquisa.
""" ),

    md("""## 5. Wait events agregados por setup

**O que mostra**: para cada setup, distribuição percentual de wait events durante runs do workload `write` (mais expressivo). Cada barra horizontal = 1 wait event, comprimento = % de amostras.

**Como ler**:
- `Lock:transactionid` dominando = row lock contention (UPDATEs disputando linhas).
- `LWLock:LockManager` = contenção INTERNA do PG nos locks. Aparece com muitos backends.
- `IO:WalSync` = commits esperando fsync do WAL.
- `CPU:CPU` (sem wait) = trabalho útil.

**Premissa do TCC**: pooler reduz `LWLock:LockManager` (menos backends competindo internamente).
"""),

    code("""act_master = master_activity(experiments)
print(f"master_activity: {len(act_master)} amostras de pg_stat_activity (todos experimentos)")

def plot_wait_events_by_setup(act_master, workload, clients_filter=None, top_n=8):
    df = act_master.copy()
    df = df[df.get("workload") == workload]
    df = df[df.get("state") == "active"]
    if clients_filter is not None:
        df = df[df.get("clients").astype(str).isin([str(c) for c in clients_filter])]
    if df.empty:
        print(f"sem dados pra workload={workload} clients={clients_filter}")
        return

    # Trata wait_event vazio como CPU
    df = df.copy()
    df["wait_event_full"] = (
        df["wait_event_type"].fillna("CPU").astype(str) + ":" +
        df["wait_event"].fillna("CPU").astype(str)
    )
    counts = (
        df.groupby(["setup", "wait_event_full"]).size()
          .reset_index(name="count")
    )
    counts["pct"] = counts.groupby("setup")["count"].transform(lambda x: 100 * x / x.sum())

    # top N wait events globais (pra eixo Y consistente)
    top_events = counts.groupby("wait_event_full")["count"].sum().nlargest(top_n).index
    counts = counts[counts["wait_event_full"].isin(top_events)]

    pivot = counts.pivot(index="wait_event_full", columns="setup", values="pct").fillna(0)
    fig, ax = plt.subplots(figsize=(9, max(4, top_n * 0.4)))
    pivot.plot(kind="barh", ax=ax)
    ax.set_xlabel("% das amostras 'active'")
    ax.set_ylabel("Wait event")
    title = f"Wait events por setup — workload={workload}"
    if clients_filter:
        title += f" (clients ∈ {clients_filter})"
    ax.set_title(title)
    ax.legend(title="Setup")
    plt.tight_layout()
    plt.show()

# Foca em concorrência intermediária (c=50) — onde diferença pooler vs none é dramática
plot_wait_events_by_setup(act_master, "write", clients_filter=[100])
plot_wait_events_by_setup(act_master, "read", clients_filter=[100])
"""),

    md("""**O que observar (write c=50)**:
- `none`: dominância massiva de `Lock:transactionid` + presença de `LWLock:LockManager`.
- `pgbouncer-tx`: mesmo Lock:transactionid mas em VOLUME muito menor; `LWLock:LockManager` deve sumir.
- **Esse é o coração da hipótese da tese**: pooler reduz contenção INTERNA do servidor.

**Em read c=50**: distribuição diferente — sem locks pesados, dominado por `Client:ClientRead` (esperando próxima query do cliente) e `CPU:CPU`.
"""),

    md("""## 6. Lock contention quantitativa

**O que mostra**: total de locks por setup × tipo (top 6). Mostra o "tamanho da fila de locks" durante o experimento, não só presença/ausência.

**Como ler**: barras altas = mais locks pendentes em média durante a janela de medição. `RowExclusiveLock` em `relation` é normal (UPDATE/INSERT precisa). `transactionid ExclusiveLock` excessivo = backends esperando uns aos outros.
"""),

    code("""locks_master = master_locks(experiments)
print(f"master_locks: {len(locks_master)} amostras de pg_locks")

def plot_locks_by_setup(locks_master, workload, top_n=6):
    df = locks_master[locks_master.get("workload") == workload]
    if df.empty:
        return
    df = df.copy()
    df["key"] = df["locktype"].astype(str) + " / " + df["mode"].astype(str)
    top = df["key"].value_counts().nlargest(top_n).index
    df = df[df["key"].isin(top)]
    counts = df.groupby(["setup", "key"]).size().reset_index(name="count")
    pivot = counts.pivot(index="key", columns="setup", values="count").fillna(0)
    fig, ax = plt.subplots(figsize=(9, max(4, top_n * 0.5)))
    pivot.plot(kind="barh", ax=ax)
    ax.set_xlabel("Total de locks observados (todas amostras agregadas)")
    ax.set_title(f"Lock contention por setup — workload={workload}")
    ax.legend(title="Setup")
    plt.tight_layout()
    plt.show()

plot_locks_by_setup(locks_master, "write")
"""),

    md("""**Esperado**:
- `relation/RowExclusiveLock` alto pra setups que rodaram (UPDATEs).
- `transactionid/ExclusiveLock` mostra backends esperando uns aos outros — DEVE ser maior em `none` que em `pgbouncer-tx` (mesma evidência da seção wait events, dimensão complementar).
"""),

    md("""## 7. Throughput de queries por setup (deltas pg_stat_statements)

**O que mostra**: soma dos deltas de `calls` (quantas vezes cada query rodou) por setup, durante a janela de medição. Aproxima TPS do servidor por DENTRO (vs pgbench client-side).

**Por que duas perspectivas?** TPS do pgbench mede do CLIENTE; calls em pg_stat_statements mede do SERVIDOR. Se diferem, há perda no transporte (pooler descartando, fila estourando).
"""),

    code("""sd = master_statements_deltas(experiments)
print(f"master_statements_deltas: {len(sd)} linhas (deltas de queries)")

def plot_calls_by_setup(sd, workload, clients_filter=None):
    df = sd[sd.get("workload") == workload]
    if clients_filter is not None:
        df = df[df.get("clients").astype(str).isin([str(c) for c in clients_filter])]
    if df.empty or "delta_calls" not in df.columns:
        print(f"sem dados pra workload={workload} clients={clients_filter}")
        return
    # Soma deltas por setup (total de calls servidas durante o experimento)
    agg = df.groupby("setup")["delta_calls"].sum().sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(8, 4))
    agg.plot(kind="bar", ax=ax, color="steelblue")
    ax.set_ylabel("Total de calls (deltas somados)")
    ax.set_title(f"Server-side throughput — workload={workload} clients={clients_filter or 'todos'}")
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.show()

plot_calls_by_setup(sd, "write", clients_filter=[100])
plot_calls_by_setup(sd, "read", clients_filter=[100])
"""),

    md("""**O que observar**: ordem entre setups deve corresponder à ordem do TPS (seção 3). Discrepância grande = collector perdeu amostras OU pooler descartou queries silenciosamente.
"""),

    md("""## 8. Pool waiting (PgBouncer)

**O que mostra**: `cl_waiting` do PgBouncer — quantos clientes ESTÃO em fila esperando backend liberar — em função do tempo, por (clients, pool_size). Só faz sentido pra setups PgBouncer.

**Como ler**:
- `cl_waiting=0` constante = pool dimensionado adequadamente pra carga.
- `cl_waiting` crescente = pool subdimensionado.
- Se `cl_waiting > 0` e `cl_active = pool_size`: pool saturado.
"""),

    code("""# Carrega CSV pgbouncer_show_pools de cada experimento PgBouncer
def gather_pgbouncer_pools(experiments):
    rows = []
    for exp_id, exp in experiments.items():
        cfg = (exp.get("config") or {}).get("label") or {}
        if not cfg or not cfg.get("setup", "").startswith("pgbouncer"):
            continue
        csv_path = DATA_DIR / exp_id / "pgbouncer_show_pools.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
        except Exception:
            continue
        # Filtra apenas o pool real (não o admin 'pgbouncer/pgbouncer')
        df = df[df["database"] == "tcc"]
        df["exp_id"] = exp_id
        for k, v in cfg.items():
            df[k] = v
        rows.append(df)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()

pools_df = gather_pgbouncer_pools(experiments)
print(f"pgbouncer_show_pools: {len(pools_df)} amostras")

if not pools_df.empty and "cl_waiting" in pools_df.columns:
    summary = (
        pools_df.assign(clients_n=pools_df["clients"].astype(int))
                .groupby(["clients_n", "setup"])
                .agg(
                    cl_waiting_max=("cl_waiting", "max"),
                    cl_active_max=("cl_active", "max"),
                    sv_active_max=("sv_active", "max"),
                    sv_idle_max=("sv_idle", "max"),
                    n_amostras=("cl_waiting", "size"),
                )
                .reset_index()
    )
    display(summary)
"""),

    md("""**O que observar**:
- Em c=500 com pool=8: `cl_waiting` deve chegar perto de 500-8=492.
- Em c=1: `cl_waiting=0` sempre, `sv_active≤1`.
- Pool nunca passa do `pool_size` configurado.
"""),

    md("""## 9. Failures arquiteturais (manifest)

Tabela dos runs marcados como `failed` ou `expected_failure`. Documenta os "regimes onde o setup não opera" — evidência negativa, mas relevante.
"""),

    code("""import csv
manifest_path = DATA_DIR / "manifest.csv"
if manifest_path.exists():
    mf = pd.read_csv(manifest_path)
    fails = mf[mf["status"].isin(["failed", "expected_failure"])]
    print(f"Falhas: {len(fails)} (de {len(mf)} runs totais)")
    if not fails.empty:
        sumario = (
            fails.groupby(["setup", "status", "clients"])
                 .size().reset_index(name="n")
                 .sort_values(["setup", "clients"])
        )
        display(sumario)
else:
    print("manifest.csv não existe — nenhuma bateria orquestrada rodada ainda")
"""),

    md("""**Esperado**:
- `none clients > 415` (max_connections): falha por "too many clients already".
- `pgpool clients > num_init_children`: bloqueio (timeout no warmup).
- `pgbouncer-*`: nunca falha por arquitetura — só por timeout em concorrências extremas.

> **Observação importante pro TCC**: esses "failures" não são bugs do experimento — são evidência empírica do limite arquitetural de cada pooler. PgBouncer enfileira indefinidamente; Pgpool bloqueia em `num_init_children`; conexão direta é limitada por `max_connections`.
"""),

    md("""## 10. Conclusões parciais (a preencher após bateria cirúrgica)

- [ ] Identificar break-even point por workload e por pooler
- [ ] Quantificar redução de `LWLock:LockManager` por concorrência (gráfico ainda não plotado)
- [ ] Avaliar impacto de `-M prepared` (PgBouncer 1.21+ feature)
- [ ] Comparar pool sizes (sub/adequado/super-dimensionado)
- [ ] Estudo de caso AWS RDS Proxy
"""),
]


def main():
    nb = {
        "cells": CELLS,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "codemirror_mode": {"name": "ipython", "version": 3},
                "file_extension": ".py",
                "mimetype": "text/x-python",
                "name": "python",
                "nbconvert_exporter": "python",
                "pygments_lexer": "ipython3",
                "version": "3.12",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    NB_PATH.write_text(json.dumps(nb, indent=1))
    print(f"Notebook gerado: {NB_PATH} ({len(CELLS)} cells)")


if __name__ == "__main__":
    main()
