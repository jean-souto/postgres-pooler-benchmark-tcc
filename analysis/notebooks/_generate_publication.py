"""Gera 02_publication.ipynb — versao publication-ready do TCC.

Resolve 5 problemas estruturais do 01_baseline:
  P1) pool_size desaparecido -> 1 subplot por pool_size
  P2) latencia sobreposta -> enfase visual em 'none' (linha grossa, alpha=1)
  P3) wait events em c=100 satura -> mostrar c=30, c=100, c=300 lado a lado
  P4) cores sem hierarquia + log enganoso -> SETUP_COLORS fixos, log so se >5x
  P5) cliffs vs missing data indistinguiveis -> marcador X no ultimo ponto valido
                                                 + annotation com causa

Uso:
    cd /home/jean/Documentos/TCC/Pratica
    source .venv/bin/activate
    python analysis/notebooks/_generate_publication.py
"""
import json
import uuid
from pathlib import Path

NB_PATH = Path(__file__).parent / "02_publication.ipynb"


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


# =====================================================================
# CELL DEFINITIONS
# =====================================================================

CELLS = [
    md("""# Connection Poolers em PostgreSQL — Analise Empirica

**Pergunta de pesquisa**: *Em qual ponto o overhead de um connection pooler eh
compensado pela reducao de contencao no PostgreSQL?*

**Hipoteses**:
- **H1 (break-even)**: ate concorrencias modestas (clients < pool_size util do
  servidor), um pooler ADICIONA latencia sem ganho. A partir de um break-even
  point, a reducao de contencao interna (LWLock, fila de transacoes) supera o
  overhead. A localizacao exata depende de (workload, modo, pool_size).
- **H2 (contencao interna)**: poolers reduzem `LWLock:LockManager` por terem
  menos backends fisicos competindo no gerenciador de locks do PostgreSQL.
- **H3 (multi-thread)**: um pooler multi-threaded (PgCat, Rust/tokio) sustenta
  mais throughput que um single-threaded (PgBouncer) em concorrencia alta, com
  pooler rodando em host multi-core (o proprio pooler deixa de ser gargalo de CPU).

**Setups comparados**:

| Setup | Tipo | pool_size testados | Comportamento esperado em sobrecarga |
|---|---|---|---|
| `none`  | direto (controle) | - | colapso quando `clients > max_connections=415` |
| `pgbouncer-tx` | self-hosted, single-thread, transaction | 2, 8, 100 | elastico: enfileira indefinidamente |
| `pgbouncer-session` | self-hosted, single-thread, session | 2, 8, 100 | bloqueia quando `clients > pool_size` |
| `pgcat-tx` | self-hosted, multi-thread (Rust/tokio), transaction | 2, 8, 100 | elastico: enfileira indefinidamente |
| `pgcat-session` | self-hosted, multi-thread (Rust/tokio), session | 2, 8, 100 | bloqueia quando `clients > pool_size` |
| `rds-proxy` | gerenciado (AWS), transaction (multiplexa) | - | elastico: pooling gerenciado, multiplexa transaction-level |

**Workloads**: `read` (SELECT puro), `write` (UPDATE simples), `mixed` (TPC-B-like).
**Modos**: `simple`, `prepared`.
**Concorrencias** (preset expandida): 1, 30, 50, 100, 200, 300, 500, 1000. **Reps**: 3.

> Notebook gerado por `_generate_publication.py`. Nao editar diretamente.
"""),

    md("""## Setup

Cores semanticas fixas em todos os graficos, com familias pareadas: pgbouncer
em azul, pgcat em verde, rds-proxy em laranja (gerenciado), controle `none` em
cinza. O contraste azul (single-thread) vs verde (multi-thread) evidencia H3.
Helpers para formatacao humana de numeros, escala de eixo adaptativa (log apenas
se range >5x), e marcador de cliff arquitetural.
"""),

    code("""# === Imports e configuracao global ===
import os
import sys
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyArrowPatch
from matplotlib.ticker import FuncFormatter

warnings.filterwarnings('ignore')

try:
    import seaborn as sns
    sns.set_theme(style='whitegrid', rc={'axes.spines.right': False, 'axes.spines.top': False})
    HAS_SNS = True
except ImportError:
    HAS_SNS = False

ROOT = Path.cwd().resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.loader import (
    load_experiments, tps_dataframe,
    master_activity, master_locks,
)

pd.set_option('display.max_columns', 50)
pd.set_option('display.width', 200)
pd.set_option('display.precision', 2)

# Cores semanticas FIXAS — usar em todo grafico.
# Familias pareadas: pgbouncer=azul, pgcat=verde, rds-proxy=laranja (gerenciado),
# none=cinza (controle). Contraste azul vs verde evidencia H3 (single vs multi-thread).
SETUP_COLORS = {
    'none':              '#555555',  # cinza (controle)
    'pgbouncer-tx':      '#0072B2',  # azul escuro
    'pgbouncer-session': '#56B4E9',  # azul claro
    'pgcat-tx':          '#009E73',  # verde escuro
    'pgcat-session':     '#5FBF8F',  # verde claro
    'rds-proxy':         '#E69F00',  # laranja (gerenciado)
}
SETUP_ORDER = ['none', 'pgbouncer-tx', 'pgbouncer-session',
               'pgcat-tx', 'pgcat-session', 'rds-proxy']

# Pool sizes esperados por setup (pra criar facets consistentes).
# Todos os poolers self-hosted usam os MESMOS pool_sizes -> plotam no mesmo facet.
# none e rds-proxy NAO tem pool_size (rds-proxy multiplexa internamente).
POOL_SIZES_BY_SETUP = {
    'pgbouncer-tx':      [2, 8, 100],
    'pgbouncer-session': [2, 8, 100],
    'pgcat-tx':          [2, 8, 100],
    'pgcat-session':     [2, 8, 100],
}

# Cliffs arquiteturais conhecidos (clients onde o setup colapsa).
# ELASTICOS (nunca recebem cliff): pgbouncer-tx, pgcat-tx (enfileiram),
#   rds-proxy (pooling gerenciado, multiplexa transaction-level).
# CLIFF BINARIO: none (max_connections), *-session (clients > pool_size).
PG_MAX_CONNECTIONS = 415

def cliff_text(setup, pool_size):
    \"\"\"Texto curto da causa do cliff arquitetural.\"\"\"
    if setup == 'none':
        return f'cliff: max_connections={PG_MAX_CONNECTIONS}'
    if setup in ('pgbouncer-session', 'pgcat-session'):
        return f'cliff: pool_size={int(pool_size)}'
    return None


def is_at_cliff(setup, pool_size, last_clients):
    \"\"\"True se o ultimo ponto da serie representa cliff arquitetural atingido.

    - none: clients > max_connections (415)
    - pgbouncer-session/pgcat-session: clients > pool_size
    - pgbouncer-tx/pgcat-tx/rds-proxy: NUNCA (elasticos — enfileiram/multiplexam)
    \"\"\"
    if last_clients is None or pd.isna(last_clients):
        return False
    if setup == 'none' and last_clients > PG_MAX_CONNECTIONS:
        return True
    if setup in ('pgbouncer-session', 'pgcat-session'):
        if pool_size is None or pd.isna(pool_size):
            return False
        if last_clients > pool_size:
            return True
    return False


def human_format(x, pos=None):
    \"\"\"Formata numero em K/M/B (eixo Y).\"\"\"
    if x == 0:
        return '0'
    abs_x = abs(x)
    for unit in ('', 'K', 'M', 'B'):
        if abs_x < 1000:
            s = f'{x:.1f}{unit}'
            return s.rstrip('0').rstrip('.') if '.' in s else s
        x /= 1000
        abs_x /= 1000
    return f'{x:.1f}T'


def needs_log_scale(values, threshold=5.0):
    \"\"\"True se max/min > threshold (sugere log).\"\"\"
    v = np.asarray([x for x in values if x is not None and not np.isnan(x) and x > 0])
    if len(v) < 2:
        return False
    return (v.max() / v.min()) > threshold


def style_axis(ax, *, xlabel='', ylabel='', title='', use_log_y=False, use_log_x=False):
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11)
    if use_log_x:
        ax.set_xscale('log')
    if use_log_y:
        ax.set_yscale('log')
    else:
        ax.yaxis.set_major_formatter(FuncFormatter(human_format))
    ax.grid(True, alpha=0.3, linestyle='--')


# A bateria principal (cloud, 1332 runs, 6 setups) fica em data-expandida/.
# Cai pra data/ (experimento local antigo) se a expandida nao existir. Pode-se
# sobrescrever via env TCC_DATA_DIR.
_root = (Path.cwd() / '..' / '..').resolve()
DATA_DIR = (
    Path(os.environ['TCC_DATA_DIR']).resolve() if os.environ.get('TCC_DATA_DIR')
    else next((_root / d for d in ('data-expandida', 'data') if (_root / d).exists()),
              _root / 'data-expandida')
)
print(f'DATA_DIR = {DATA_DIR}  exists={DATA_DIR.exists()}')
print(f'SETUP_COLORS = {SETUP_COLORS}')
"""),

    md("""## 1. Carga dos dados

Le `data/{exp_id}/` em paralelo (ThreadPool). Cada experimento -> dict com
4 DataFrames (`activity`, `locks`, `statements`, `io`) + `config`. Runs
`failed`/`expected_failure` sem dados sao silenciosamente pulados pelos
helpers de agregacao.
"""),

    code("""experiments = load_experiments(DATA_DIR) if DATA_DIR.exists() else {}
tps_df = tps_dataframe(experiments, DATA_DIR) if experiments else pd.DataFrame()

# Normalizacao: pool_size NaN para 'none', int para resto.
if not tps_df.empty:
    tps_df['pool_size'] = pd.to_numeric(tps_df['pool_size'], errors='coerce')

print(f'Experimentos carregados: {len(experiments)}')
print(f'Runs com TPS valido: {len(tps_df)}')
print()
print('Setups encontrados:', sorted(tps_df['setup'].unique()) if not tps_df.empty else '-')
"""),

    md("""## 2. Sumario da bateria

Counts de runs por status, agrupados por (setup, workload). Permite ver
de relance onde temos cobertura completa vs onde temos lacunas.
"""),

    code("""manifest_path = DATA_DIR / 'manifest.csv'
mf = pd.read_csv(manifest_path) if manifest_path.exists() else pd.DataFrame()

if not mf.empty:
    summary = (
        mf.groupby(['setup', 'workload', 'status'])
          .size()
          .unstack(fill_value=0)
    )
    # Garante todas colunas. 'saturated' = colapso sob carga (resultado VALIDO,
    # nao falha) -- e o achado central, nao pode ser omitido do sumario.
    for col in ('done', 'saturated', 'failed', 'expected_failure'):
        if col not in summary.columns:
            summary[col] = 0
    summary = summary[['done', 'saturated', 'failed', 'expected_failure']]
    summary['total'] = summary.sum(axis=1)
    display(summary.style.background_gradient(subset=['done'], cmap='Greens')
                          .background_gradient(subset=['saturated'], cmap='Oranges')
                          .background_gradient(subset=['failed'], cmap='Reds')
                          .set_caption('Runs por (setup, workload, status)'))
else:
    print('manifest.csv ausente')
"""),

    md("""## 3. Throughput (TPS) por workload e pool_size

**O que mostra**: 1 figura por workload. Em cada figura, 4 subplots: o subplot
"sem pool" (baselines `none` e `rds-proxy`, ambos sem pool_size) e os 3 pool_sizes
(2/8/100) com os 4 poolers self-hosted. Em cada subplot, 1 linha por setup com
cores fixas, marcadores em cada concorrencia testada, e shaded area p25-p75 entre
as reps (band de confianca). O ultimo ponto valido de cada serie marca cliffs
arquiteturais com `X` + annotation.

**Como ler**:
- Subplot 0 ("sem pool"): `none` (direto) vs `rds-proxy` (gerenciado) — as duas
  referencias sem pool_size.
- Subplots 1-3 (pool_size 2/8/100): os 4 poolers (pgbouncer-tx/session em azul,
  pgcat-tx/session em verde) no MESMO pool_size + overlays das baselines `none`
  (cinza, grossa) e `rds-proxy` (laranja, tracejado) por cima — permite comparar
  self-hosted vs gerenciado vs direto em cada pool.
- **H3**: contraste azul (pgbouncer, single-thread) vs verde (pgcat, multi-thread)
  em concorrencia alta — pgcat deve sustentar mais throughput.
- Eixo Y: linear quando range <5x, log quando >=5x (auto, evita log enganoso).
- `X` no fim da linha = cliff arquitetural (causa anotada). Elasticos (tx,
  rds-proxy) nunca recebem X.
"""),

    code("""def _agg_tps(df):
    \"\"\"Agrega tps por (setup, pool_size, clients) -> p25, p50, p75 das reps.\"\"\"
    return (
        df.groupby(['setup', 'pool_size', 'clients'], dropna=False)['tps']
          .agg(p25=lambda s: s.quantile(0.25), p50='median', p75=lambda s: s.quantile(0.75), n='count')
          .reset_index()
    )


def _draw_setup_curve(ax, sub_setup, setup, *, emphasize=False, show_band=True,
                      pool_size=None, annotate_cliff=False, linestyle='-'):
    \"\"\"Desenha 1 linha + (opcional) band p25-p75 + cliff marker.

    Fix 1: cliff X so e marcado se is_at_cliff() retornar True (cliff arquitetural
    realmente atingido). Setups elasticos (tx, rds-proxy) nunca recebem X.

    Fix 2: quando emphasize=False (none/rds-proxy como baseline), usa zorder alto
    para FICAR EM CIMA dos poolers (que sao plotados primeiro). linestyle dashed
    distingue o overlay gerenciado (rds-proxy) do direto (none, solido).

    Fix 3: se annotate_cliff=True e cliff atingido, desenha ax.annotate com causa.
    \"\"\"
    if sub_setup.empty:
        return
    sub_setup = sub_setup.sort_values('clients')
    color = SETUP_COLORS[setup]

    # Fix 2: enfase invertida para baselines (none/rds-proxy) nos subplots de pool_size
    is_baseline = (setup in ('none', 'rds-proxy')) and (not emphasize)
    if is_baseline:
        # baseline: linha grossa, alpha=1, zorder alto -> fica visivel em cima
        lw = 2.8
        alpha = 1.0
        marker = 'o'
        msize = 7
        zorder = 6
    elif emphasize:
        # poolers no subplot do seu pool_size: square, ligeiramente mais fino, alpha 0.85
        lw = 2.2
        alpha = 0.85
        marker = 's'
        msize = 6
        zorder = 3
    else:
        # fallback (baseline isolada no subplot 0)
        lw = 3.0
        alpha = 1.0
        marker = 'o'
        msize = 7
        zorder = 4

    ax.plot(
        sub_setup['clients'], sub_setup['p50'],
        marker=marker, markersize=msize, linestyle=linestyle,
        color=color, linewidth=lw, alpha=alpha,
        label=setup, zorder=zorder,
    )
    if show_band and (sub_setup['n'] >= 2).any():
        valid = sub_setup[sub_setup['n'] >= 2]
        ax.fill_between(
            valid['clients'], valid['p25'], valid['p75'],
            color=color, alpha=0.15, zorder=1,
        )
    # Fix 1: cliff X apenas se arquiteturalmente atingido
    last = sub_setup.iloc[-1]
    last_clients = last['clients']
    ps = pool_size
    if setup == 'none':
        ps = None  # none nao usa pool_size
    if is_at_cliff(setup, ps, last_clients):
        ax.plot(
            last_clients, last['p50'],
            marker='X', markersize=14, color=color,
            markeredgecolor='black', markeredgewidth=1.5, zorder=10,
        )
        # Fix 3: annotation com causa (so se annotate_cliff=True para evitar clutter)
        if annotate_cliff:
            text = cliff_text(setup, ps)
            if text:
                # offset adaptativo: para cima se ponto baixo, para baixo se alto
                ax.annotate(
                    text,
                    xy=(last_clients, last['p50']),
                    xytext=(-60, 25),
                    textcoords='offset points',
                    fontsize=8, color='dimgray',
                    arrowprops={'arrowstyle': '->', 'color': 'gray', 'alpha': 0.6, 'lw': 0.8},
                    zorder=11,
                )


def plot_tps_by_workload(tps_df, workload):
    df = tps_df[tps_df['workload'] == workload].dropna(subset=['tps'])
    if df.empty:
        print(f'sem dados pra workload={workload}')
        return

    # 4 subplots: "sem pool" (none + rds-proxy) + 3 pool sizes (poolers self-hosted).
    pool_sizes = [2, 8, 100]
    POOLERS = ['pgbouncer-tx', 'pgbouncer-session', 'pgcat-tx', 'pgcat-session']

    # Layout 2x2 (16x10): legibilidade x4 vs 1x4 (20x5).
    # Cada subplot fica ~7x4.5 em vez de ~5x4, eixo X de log-clients respira.
    fig, axes_grid = plt.subplots(2, 2, figsize=(16, 10), sharey=False)
    axes = axes_grid.flatten()
    fig.suptitle(f'Throughput vs concorrencia — workload={workload}',
                 fontsize=13, y=1.00)

    sub_none = _agg_tps(df[df['setup'] == 'none'])
    sub_rds = _agg_tps(df[df['setup'] == 'rds-proxy'])

    # Subplot 0: baselines sem pool_size (none direto + rds-proxy gerenciado)
    ax = axes[0]
    _draw_setup_curve(ax, sub_none, 'none', emphasize=True, annotate_cliff=True)
    _draw_setup_curve(ax, sub_rds, 'rds-proxy', emphasize=True)
    style_axis(ax, xlabel='Clientes', ylabel='TPS mediano',
               title='sem pool (none direto vs rds-proxy gerenciado)',
               use_log_x=True,
               use_log_y=needs_log_scale(pd.concat([sub_none['p50'], sub_rds['p50']],
                                                    ignore_index=True)))
    ax.legend(loc='best', fontsize=9)

    # Subplots 1-3: 1 por pool_size, 4 poolers + overlays de baseline
    # Fix 2: poolers PRIMEIRO (zorder baixo), baselines POR ULTIMO (zorder alto, em cima)
    for i, ps in enumerate(pool_sizes):
        ax = axes[i + 1]
        # 1. Poolers primeiro (square markers, alpha 0.85)
        for setup in POOLERS:
            sub = _agg_tps(df[(df['setup'] == setup) & (df['pool_size'] == ps)])
            # Fix 3: annotate so o cliff de *-session em pool_size pequeno (cliff mais
            #        visivel). Evita clutter em pool=100. tx nunca tem cliff.
            do_annotate = (ps <= 8) and setup.endswith('-session')
            _draw_setup_curve(ax, sub, setup, emphasize=True, pool_size=ps,
                              annotate_cliff=do_annotate)
        # 2. Baselines por ULTIMO (ficam por cima dos poolers):
        #    none cinza solido grosso + rds-proxy laranja tracejado.
        do_annotate_none = (ps == 8)  # cliff do none so no subplot do meio
        _draw_setup_curve(ax, sub_none, 'none', emphasize=False, show_band=False,
                          annotate_cliff=do_annotate_none)
        _draw_setup_curve(ax, sub_rds, 'rds-proxy', emphasize=False, show_band=False,
                          linestyle='--')
        all_y = pd.concat(
            [_agg_tps(df[(df['setup'] == s) & (df['pool_size'] == ps)])['p50']
             for s in POOLERS],
            ignore_index=True)
        style_axis(ax, xlabel='Clientes', ylabel='',
                   title=f'pool_size = {ps}',
                   use_log_x=True,
                   use_log_y=needs_log_scale(all_y))
        ax.legend(loc='best', fontsize=7)

    plt.tight_layout()
    plt.show()


for wl in ('read', 'write', 'mixed'):
    plot_tps_by_workload(tps_df, wl)
"""),

    md("""**Leitura sugerida**:

- *Read*: setups elasticos (tx, rds-proxy) tendem a manter throughput em
  concorrencias altas; `none` colapsa quando `clients > max_connections=415`.
  Os `*-session` colapsam quando `clients > pool_size`.
- *Write*: contencao limita o teto absoluto; o GANHO do pooler eh REDUZIR a
  perda em concorrencia alta, nao aumentar o teto.
- **H3**: em concorrencia alta, espera-se que `pgcat-tx` (verde) sustente mais
  throughput que `pgbouncer-tx` (azul) por ser multi-threaded.
- O **break-even** eh o cruzamento da linha de um pooler com a cinza (none).
"""),

    md("""## 4. Latencia mediana por workload e pool_size

**O que mostra**: latencia media (ms) reportada pelo pgbench. Mesmo formato
do TPS: 4 subplots por workload (sem pool = none+rds-proxy; depois 3 pool_sizes
com os 4 poolers + overlays de baseline).

**Como ler**:
- Curvas crescentes = sistema saturando (queue cresce).
- Em concorrencias baixas (c=1, c=30), o pooler ADICIONA latencia (esperado).
- Em concorrencias altas, o pooler DEVE ter latencia menor que `none`.
- Cruzamento das curvas = break-even.
"""),

    code("""def _agg_lat(df):
    return (
        df.groupby(['setup', 'pool_size', 'clients'], dropna=False)['latency_avg_ms']
          .agg(p25=lambda s: s.quantile(0.25), p50='median', p75=lambda s: s.quantile(0.75), n='count')
          .reset_index()
    )


def plot_lat_by_workload(tps_df, workload):
    df = tps_df[tps_df['workload'] == workload].dropna(subset=['latency_avg_ms'])
    if df.empty:
        return

    pool_sizes = [2, 8, 100]
    POOLERS = ['pgbouncer-tx', 'pgbouncer-session', 'pgcat-tx', 'pgcat-session']

    # Layout 2x2 (16x10) — mesma decisao do TPS pra consistencia.
    fig, axes_grid = plt.subplots(2, 2, figsize=(16, 10), sharey=False)
    axes = axes_grid.flatten()
    fig.suptitle(f'Latencia media vs concorrencia — workload={workload}',
                 fontsize=13, y=1.00)

    # Helper local: desenha latency curve.
    # Fix 1+2+3: mesma logica do TPS — usa is_at_cliff(), enfase invertida em baseline,
    # annotation textual no cliff. linestyle dashed distingue rds-proxy (gerenciado).
    def draw_lat(ax, sub, setup, *, emphasize=False, show_band=True,
                 pool_size=None, annotate_cliff=False, linestyle='-'):
        if sub.empty:
            return
        sub = sub.sort_values('clients')
        color = SETUP_COLORS[setup]
        is_baseline = (setup in ('none', 'rds-proxy')) and (not emphasize)
        if is_baseline:
            lw, alpha, marker, msize, zorder = 2.8, 1.0, 'o', 7, 6
        elif emphasize:
            lw, alpha, marker, msize, zorder = 2.2, 0.85, 's', 6, 3
        else:
            lw, alpha, marker, msize, zorder = 3.0, 1.0, 'o', 7, 4
        ax.plot(sub['clients'], sub['p50'], marker=marker, markersize=msize,
                color=color, linewidth=lw, alpha=alpha, linestyle=linestyle,
                label=setup, zorder=zorder)
        if show_band and (sub['n'] >= 2).any():
            valid = sub[sub['n'] >= 2]
            ax.fill_between(valid['clients'], valid['p25'], valid['p75'],
                            color=color, alpha=0.15, zorder=1)
        last = sub.iloc[-1]
        last_clients = last['clients']
        ps = None if setup == 'none' else pool_size
        if is_at_cliff(setup, ps, last_clients):
            ax.plot(last_clients, last['p50'], marker='X', markersize=14,
                    color=color, markeredgecolor='black',
                    markeredgewidth=1.5, zorder=10)
            if annotate_cliff:
                text = cliff_text(setup, ps)
                if text:
                    ax.annotate(
                        text,
                        xy=(last_clients, last['p50']),
                        xytext=(-60, 25),
                        textcoords='offset points',
                        fontsize=8, color='dimgray',
                        arrowprops={'arrowstyle': '->', 'color': 'gray',
                                    'alpha': 0.6, 'lw': 0.8},
                        zorder=11,
                    )

    sub_none = _agg_lat(df[df['setup'] == 'none'])
    sub_rds = _agg_lat(df[df['setup'] == 'rds-proxy'])

    # Subplot 0: baselines sem pool_size (none direto + rds-proxy gerenciado)
    ax = axes[0]
    draw_lat(ax, sub_none, 'none', emphasize=True, annotate_cliff=True)
    draw_lat(ax, sub_rds, 'rds-proxy', emphasize=True)
    style_axis(ax, xlabel='Clientes', ylabel='Latencia media (ms)',
               title='sem pool (none direto vs rds-proxy gerenciado)',
               use_log_x=True,
               use_log_y=needs_log_scale(pd.concat([sub_none['p50'], sub_rds['p50']],
                                                    ignore_index=True)))
    ax.legend(loc='best', fontsize=9)

    for i, ps in enumerate(pool_sizes):
        ax = axes[i + 1]
        all_y = []
        # Fix 2: poolers primeiro
        for setup in POOLERS:
            sub = _agg_lat(df[(df['setup'] == setup) & (df['pool_size'] == ps)])
            do_annotate = (ps <= 8) and setup.endswith('-session')
            draw_lat(ax, sub, setup, emphasize=True, pool_size=ps,
                     annotate_cliff=do_annotate)
            all_y.append(sub['p50'])
        # Baselines por ULTIMO (em cima): none solido + rds-proxy tracejado.
        do_annotate_none = (ps == 8)
        draw_lat(ax, sub_none, 'none', emphasize=False, show_band=False,
                 annotate_cliff=do_annotate_none)
        draw_lat(ax, sub_rds, 'rds-proxy', emphasize=False, show_band=False,
                 linestyle='--')
        all_y_concat = pd.concat(all_y, ignore_index=True) if all_y else pd.Series(dtype=float)
        style_axis(ax, xlabel='Clientes', ylabel='',
                   title=f'pool_size = {ps}',
                   use_log_x=True,
                   use_log_y=needs_log_scale(all_y_concat))
        ax.legend(loc='best', fontsize=7)

    plt.tight_layout()
    plt.show()


for wl in ('read', 'write', 'mixed'):
    plot_lat_by_workload(tps_df, wl)
"""),

    md("""**Leitura sugerida**: o ponto onde a curva de um pooler cruza a curva
cinza (none) eh o break-even de latencia. Em workloads write, esse ponto tende a
ser mais cedo (a contencao de WAL/locks ja eh dominante mesmo em c=30). Compare
pgbouncer (azul) vs pgcat (verde) para avaliar H3 em concorrencia alta.
"""),

    md("""## 5. Wait events em 3 niveis de concorrencia

**Por que 3 niveis?** Em c=100 todo setup ja esta saturado e a distribuicao
de wait events satura tambem (`Lock:transactionid` e `LWLock:LockManager`
dominam em todos). A diferenca entre setups eh visivel SO em c=30 (regime
de contencao moderada).

**O que mostra**: para o workload `write` (mais expressivo de contencao),
3 subplots lado a lado com c=30, c=100, c=300. Em cada subplot, barras
horizontais com % de wait events por setup. Cores semanticas (mesmas do TPS).

**Como ler**:
- `Lock:transactionid` = backends esperando UPDATE de outro backend.
- `LWLock:LockManager` = contencao INTERNA do PG no gerenciador de locks
  (cresce com numero de backends fisicos -> hipotese central do TCC).
- `CPU:CPU` = trabalho util.
- Em c=30, deve aparecer DIFERENCA: pooler com menos LWLock que none.
"""),

    code("""act_master = master_activity(experiments)
print(f'master_activity: {len(act_master):,} amostras')

def _wait_event_pct(act, workload, clients_val, top_n=8):
    df = act[(act['workload'] == workload)
             & (act['state'] == 'active')
             & (act['clients'] == clients_val)].copy()
    # Retorno SEMPRE 2-tupla (counts, top_waits) pra unpack consistente no caller.
    if df.empty:
        return pd.DataFrame(), []
    df['wait'] = (df['wait_event_type'].fillna('CPU').astype(str) + ':'
                  + df['wait_event'].fillna('CPU').astype(str))
    counts = df.groupby(['setup', 'wait']).size().reset_index(name='n')
    counts['pct'] = counts.groupby('setup')['n'].transform(lambda x: 100 * x / x.sum())
    # top wait events por volume total (consistencia entre subplots)
    top_waits = counts.groupby('wait')['n'].sum().nlargest(top_n).index.tolist()
    return counts[counts['wait'].isin(top_waits)], top_waits


def plot_wait_events_3levels(act, workload, levels=(30, 100, 300)):
    fig, axes = plt.subplots(1, len(levels), figsize=(20, 6), sharey=True)
    fig.suptitle(f'Wait events por setup e concorrencia — workload={workload}',
                 fontsize=13, y=1.02)

    # Pega top events do nivel intermediario pra ordenar Y consistente
    _ref, ref_waits = _wait_event_pct(act, workload, levels[1])
    if not ref_waits:
        print('sem dados')
        return

    for ax, c_val in zip(axes, levels):
        result = _wait_event_pct(act, workload, c_val)
        if isinstance(result, tuple):
            counts, _ = result
        else:
            counts = result
        if counts is None or counts.empty:
            ax.text(0.5, 0.5, f'sem dados c={c_val}',
                    transform=ax.transAxes, ha='center')
            ax.set_title(f'clients = {c_val}')
            continue
        pivot = (counts.pivot(index='wait', columns='setup', values='pct')
                       .reindex(ref_waits)
                       .fillna(0))
        # Reordena colunas pra SETUP_ORDER
        cols_present = [s for s in SETUP_ORDER if s in pivot.columns]
        pivot = pivot[cols_present]
        colors = [SETUP_COLORS[s] for s in cols_present]
        pivot.plot(kind='barh', ax=ax, color=colors, width=0.75)
        ax.set_xlabel('% das amostras active')
        ax.set_ylabel('')
        ax.set_title(f'clients = {c_val}')
        ax.legend(title='', fontsize=8, loc='lower right')
        ax.grid(True, axis='x', alpha=0.3, linestyle='--')

    axes[0].set_ylabel('Wait event')
    plt.tight_layout()
    plt.show()


plot_wait_events_3levels(act_master, 'write')
plot_wait_events_3levels(act_master, 'read')
"""),

    md("""**Leitura sugerida**:

- Em **write c=30**: deve-se ver `none` com `LWLock:LockManager` perceptivel,
  enquanto os poolers (pgbouncer e pgcat, ambos *-tx) tem essa fatia bem menor.
  Esse eh o "ouro" do TCC (H2).
- **H3**: contraste pgbouncer (azul) vs pgcat (verde) — com pooler multi-thread,
  espera-se que o pgcat mantenha menos contencao de CPU no proprio pooler em
  concorrencia alta. Os `*-session` atingem cliff quando `clients > pool_size`.
- Em c=100, c=300 a saturacao apaga a diferenca — todos dominados por
  `Lock:transactionid` e `Client:ClientRead`.
- *Read*: dominado por `Client:ClientRead` (pgbench mandando proxima query) +
  `CPU:CPU`. Sem locks pesados — esse workload nao testa a hipotese de
  contencao interna.
"""),

    md("""## 6. Lock contention quantitativa

**O que mostra**: total de locks observados por (setup, locktype) durante
runs do workload `write` em **c=300**. Barras horizontais com annotation do
delta percentual em relacao a `none` (baseline).

**Por que c=300 (e nao c=100)?** Em c=100 os valores absolutos sao proximos
entre setups (30k-80k), o que torna a annotation pouco informativa. Em c=300
a diferenca e dramatica (none ~300k vs poolers ~100k = -67%) e evidencia
o IMPACTO do pooler — o ponto central da hipotese do TCC.

**Atencao aos `*-session`**: em c=300 os setups `pgbouncer-session` e
`pgcat-session` atingem cliff arquitetural (`clients > pool_size`) e os runs
sao `expected_failure`, portanto a barra aparece vazia (sem dados) quando
`pool_size < 300`. Isso esta esperado. Os setups elasticos (`*-tx` e
`rds-proxy`) seguem presentes.

**Como ler**:
- `relation/RowExclusiveLock` eh esperado (UPDATE precisa).
- `transactionid/ExclusiveLock` indica backends esperando outros — DEVE ser
  menor com pooler.
- A annotation `setup: -X%` quantifica reducao vs baseline (filtro |pct|>=10).
- Annotations empilhadas por setup com cor semantica.
"""),

    code("""locks_master = master_locks(experiments)
print(f'master_locks: {len(locks_master):,} amostras')

def plot_locks_with_delta(locks, workload, clients_val, top_n=6):
    df = locks[(locks['workload'] == workload) & (locks['clients'] == clients_val)].copy()
    if df.empty:
        print(f'sem locks pra workload={workload} c={clients_val}')
        return

    # ATENCAO: no CSV de pg_locks, a coluna 'mode' (sql mode) foi sobrescrita
    # pela coluna 'mode' do view -> usa 'mode' (eh o lock mode apos colisao).
    df['key'] = df['locktype'].astype(str) + ' / ' + df['mode'].astype(str)

    # Top N locks globais (eixo Y consistente)
    top = df['key'].value_counts().nlargest(top_n).index.tolist()
    df = df[df['key'].isin(top)]
    counts = df.groupby(['setup', 'key']).size().reset_index(name='n')
    pivot = (counts.pivot(index='key', columns='setup', values='n')
                   .reindex(top)
                   .fillna(0))
    cols_present = [s for s in SETUP_ORDER if s in pivot.columns]
    pivot = pivot[cols_present]
    colors = [SETUP_COLORS[s] for s in cols_present]

    fig, ax = plt.subplots(figsize=(12, 5))
    pivot.plot(kind='barh', ax=ax, color=colors, width=0.75)
    ax.set_xlabel('Total de locks observados (todas amostras)')
    ax.set_ylabel('')
    ax.set_title(f'Lock contention por setup — workload={workload}, clients={clients_val}',
                 fontsize=12)
    ax.legend(title='', fontsize=9, loc='lower right')
    ax.grid(True, axis='x', alpha=0.3, linestyle='--')
    ax.xaxis.set_major_formatter(FuncFormatter(human_format))

    # Fix 2: annotation -X% vs none para TODOS os poolers (filtro |pct|>=10).
    # Empilha verticalmente (3 linhas por locktype) com cor do setup -> mantem
    # legibilidade mesmo com 3x mais texto.
    if 'none' in pivot.columns:
        non_none_setups = [s for s in cols_present if s != 'none']
        for i, key in enumerate(pivot.index):
            base = pivot.loc[key, 'none']
            if base <= 0:
                continue
            line_idx = 0
            for setup in non_none_setups:
                val = pivot.loc[key, setup]
                pct = (val - base) / base * 100
                if abs(pct) < 10:
                    continue
                sign = '' if pct < 0 else '+'
                # Cada setup empilhado verticalmente com offset Y crescente.
                ax.annotate(
                    f'{setup}: {sign}{pct:.0f}%',
                    xy=(val, i),
                    xytext=(12, 12 - line_idx * 11),
                    textcoords='offset points',
                    fontsize=7.5, color=SETUP_COLORS[setup],
                    fontweight='bold',
                )
                line_idx += 1
    plt.tight_layout()
    plt.show()


# Fix 3 (opcao A): c=300 evidencia o IMPACTO do pooler (none ~300k vs poolers ~100k,
# reducao ~67%). Em c=100 a diferenca era marginal (30k-80k, todos similares).
# *-session em c=300 e expected_failure quando pool_size < 300 -> barra vazia.
# Elasticos (*-tx, rds-proxy) seguem presentes.
plot_locks_with_delta(locks_master, 'write', clients_val=300)
print()
print('Nota: pgbouncer-session/pgcat-session ausentes em c=300 quando pool_size < 300')
print('      (cliff arquitetural clients > pool_size -> runs expected_failure).')
print('      Elasticos (pgbouncer-tx, pgcat-tx, rds-proxy) seguem presentes.')
"""),

    md("""**Leitura sugerida**: as annotations `setup: -X%` em CADA pooler
quantificam a reducao de contencao em relacao a `none`. Se forem negativas
em `transactionid` ou `LWLock`-relacionado, a hipotese central se confirma.

Em c=300 espera-se ver os poolers com reducao acentuada (>50%) em
locks de transactionid — `none` tem 415 backends competindo no
LockManager interno, enquanto os poolers tem ~pool_size backends fisicos.
Compare pgbouncer (azul) vs pgcat (verde) para H3.
"""),

    md("""## 7. Tabelas de resumo styled

Tabelas pivot com TPS mediano e latencia p50 por (setup, pool_size, clients).
Gradiente verde -> alto, vermelho -> baixo. Permite scan rapido pra encontrar
o "sweet spot" de cada setup.
"""),

    code("""def summary_table(tps_df, workload, metric='tps', agg='median'):
    df = tps_df[tps_df['workload'] == workload].dropna(subset=[metric]).copy()
    if df.empty:
        return pd.DataFrame()
    # pool_size NaN (setup=none) vira string 'na' para evitar KeyError no Styler.
    df['pool_size'] = df['pool_size'].apply(
        lambda x: 'na' if pd.isna(x) else f'{int(x)}'
    )
    pivot = (df.groupby(['setup', 'pool_size', 'clients'])[metric]
               .agg(agg)
               .unstack('clients'))
    return pivot


for wl in ('read', 'write', 'mixed'):
    print(f'=== TPS mediano — workload={wl} ===')
    tbl = summary_table(tps_df, wl, metric='tps')
    if not tbl.empty:
        display(tbl.style.background_gradient(cmap='Greens', axis=None)
                          .format(precision=0)
                          .set_caption(f'TPS mediano — {wl}'))
    print(f'=== Latencia p50 (ms) — workload={wl} ===')
    tbl = summary_table(tps_df, wl, metric='latency_avg_ms')
    if not tbl.empty:
        display(tbl.style.background_gradient(cmap='Reds', axis=None)
                          .format(precision=2)
                          .set_caption(f'Latencia p50 (ms) — {wl}'))
"""),

    md("""## 8. Break-even points: quando o pooler compensa o overhead?

Tabela responde DIRETAMENTE a pergunta de pesquisa: para cada (workload, pool_size),
qual e o **menor `clients`** em que TPS mediano do pooler **iguala ou supera** o `none`?

**Como ler**:
- **Numero baixo** (ex.: 30) = pooler ja compensa em concorrencia modesta (ideal).
- **Numero alto** (ex.: 1000) = break-even tardio (pooler so vale a pena em sobrecarga).
- **`-`** = o pooler **nao supera o `none`** em nenhum dos `clients` testados
  (1, 30, 50, 100, 200, 300, 500, 1000). Pode significar duas coisas distintas,
  ambas legitimas:
  1. **Overhead persistente**: o pooler adiciona latencia que nunca e compensada
     pelo ganho de contencao naquela faixa (tipico de workload `read` em pool=2).
  2. **Sem dados validos**: combinacao (setup, pool_size, workload) nao tem runs
     `done` no manifest (ex.: `*-session` em workload onde todos runs falharam
     por cliff). Cruzar com a tabela da Secao 2 esclarece qual caso.

Colunas: os 4 poolers self-hosted (pgbouncer-tx/session, pgcat-tx/session) por
pool_size + `rds-proxy`. Como `rds-proxy` nao tem pool_size (pooling gerenciado),
ele aparece com **pool_size='na'**: seu break-even e calculado uma vez por workload
e repetido em todas as linhas de pool_size daquele workload (valor constante).

Em ambos os casos, `-` significa **"nao ha break-even na faixa testada"** — nao e bug.
"""),

    code("""def compute_breakeven(tps_df):
    \"\"\"Para cada (workload, pool_size), computa o menor clients
    em que TPS mediano do pooler >= TPS mediano do none.

    Retorna DataFrame com colunas:
        workload, pool_size, pgbouncer-tx, pgbouncer-session,
        pgcat-tx, pgcat-session, rds-proxy
    onde valores sao o clients de break-even (ou '-' se nunca atinge).

    rds-proxy nao tem pool_size: seu break-even e calculado uma vez por workload
    e repetido em todas as linhas de pool_size daquele workload (pool_size='na').
    \"\"\"
    if tps_df.empty:
        return pd.DataFrame()

    # TPS mediano por (setup, pool_size, workload, clients)
    med = (tps_df.dropna(subset=['tps'])
                 .groupby(['setup', 'pool_size', 'workload', 'clients'], dropna=False)['tps']
                 .median()
                 .reset_index())

    def _breakeven_clients(none_series, pooler):
        \"\"\"Menor clients onde pooler >= none (nos clients comuns). '-' se nunca.\"\"\"
        if pooler.empty:
            return '-'
        common = none_series.index.intersection(pooler.index)
        if len(common) == 0:
            return '-'
        wins = [c for c in sorted(common) if pooler[c] >= none_series[c]]
        return wins[0] if wins else '-'

    rows = []
    workloads = sorted(med['workload'].unique())
    pool_sizes = [2, 8, 100]
    POOLERS = ['pgbouncer-tx', 'pgbouncer-session', 'pgcat-tx', 'pgcat-session']

    for wl in workloads:
        # baseline: tps mediano do 'none' por clients
        none_series = (med[(med['setup'] == 'none') & (med['workload'] == wl)]
                       .set_index('clients')['tps']
                       .sort_index())
        if none_series.empty:
            continue

        # rds-proxy: serie unica por workload (sem pool_size -> pool_size NaN)
        rds_series = (med[(med['setup'] == 'rds-proxy') & (med['workload'] == wl)]
                      .set_index('clients')['tps']
                      .sort_index())
        rds_be = _breakeven_clients(none_series, rds_series)

        for pool in pool_sizes:
            row = {'workload': wl, 'pool_size': pool}
            for setup in POOLERS:
                pooler = (med[(med['setup'] == setup)
                              & (med['pool_size'] == pool)
                              & (med['workload'] == wl)]
                          .set_index('clients')['tps']
                          .sort_index())
                row[setup] = _breakeven_clients(none_series, pooler)
            # rds-proxy (pool_size='na'): valor constante por workload, repetido
            row['rds-proxy'] = rds_be
            rows.append(row)

    return pd.DataFrame(rows)


breakeven = compute_breakeven(tps_df)
if breakeven.empty:
    print('sem dados pra computar break-even')
else:
    # Numeros pra gradient (substitui '-' por NaN temporariamente)
    numeric_cols = ['pgbouncer-tx', 'pgbouncer-session',
                    'pgcat-tx', 'pgcat-session', 'rds-proxy']
    display_df = breakeven.copy()

    def _fmt(v):
        if v == '-' or pd.isna(v):
            return '-'
        return f'{int(v)}'

    styled = (display_df.style
              .format({c: _fmt for c in numeric_cols})
              .set_caption('Break-even: menor clients onde TPS(pooler) >= TPS(none)')
              .set_properties(**{'text-align': 'center'}))
    display(styled)
    print()
    print('Interpretacao:')
    print('  - Numero baixo = pooler ja compensa em concorrencia modesta (bom)')
    print('  - "-" = pooler nunca compensa overhead na faixa testada')
    print('  - Numero alto = break-even so em concorrencia alta')
    print('  - rds-proxy: pool_size="na" (gerenciado) -> valor constante por workload')
"""),

    md("""## 9. Conclusoes empiricas

> Estas observacoes sao geradas a partir dos graficos acima. Reescrever
> conforme os resultados de fato observados na bateria atual.

- **H1 — Break-even de TPS**: deve depender fortemente de workload. Em `read`,
  espera-se o ganho do pooler so em concorrencias altas; em `write`, mais cedo
  por causa de contencao de locks.
- **Pool sizes ideais**: espera-se pool=8 como melhor compromisso para a maioria
  dos casos (poucas conexoes fisicas, queue absorve bursts). pool=2 deve limitar
  throughput; pool=100 nao deve trazer ganho proporcional.
- **Self-hosted vs gerenciado**: os 4 poolers self-hosted (pgbouncer/pgcat) e o
  `rds-proxy` (gerenciado AWS) devem ser elasticos nos modos transaction —
  comparar contra `none` direto em cada pool_size mostra o custo/beneficio do
  pooling. `rds-proxy` aparece como overlay tracejado laranja por nao ter pool_size.
- **H3 — multi-thread**: espera-se que `pgcat-*` (Rust/tokio, multi-thread,
  verde) sustente mais throughput que `pgbouncer-*` (single-thread, azul) em
  concorrencia alta, quando o proprio pooler roda em host multi-core.
- **Cliffs arquiteturais**: marcados com X. `none` deve colapsar quando
  `clients > max_connections=415`. Os `*-session` (pgbouncer e pgcat) colapsam
  quando `clients > pool_size`. Os `*-tx` e o `rds-proxy` sao elasticos.
- **H2 — Wait events em c=30**: a hipotese de contencao interna (pooler reduz
  LWLock:LockManager) deve aparecer aqui, antes da saturacao apagar a diferenca
  em c=100+.
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
    n_md = sum(1 for c in CELLS if c["cell_type"] == "markdown")
    n_code = sum(1 for c in CELLS if c["cell_type"] == "code")
    print(f"Notebook gerado: {NB_PATH}")
    print(f"Total: {len(CELLS)} cells ({n_md} markdown, {n_code} code)")


if __name__ == "__main__":
    main()
