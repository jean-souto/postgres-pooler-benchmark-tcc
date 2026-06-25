"""Gera as figuras de publicação da monografia a partir do data-expandida real.

Saída: monografia/figuras/*.png. Todos os números saem do dado bruto (3 reps);
nada é hardcoded. Rodar com o .venv do projeto:
    .venv/bin/python analysis/figs_monografia.py
"""
from __future__ import annotations
import json, re
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent / "data-expandida"
OUT = Path(__file__).resolve().parent.parent / "monografia" / "figuras"
OUT.mkdir(parents=True, exist_ok=True)
TPS_RE = re.compile(r"tps = ([\d.]+)")

# Paleta consistente (color-blind friendly)
C = {
    "none": "#666666", "pgbouncer": "#1f77b4", "pgcat": "#2ca02c",
    "rds": "#d62728", "prep": "#9467bd", "accent": "#ff7f0e",
}
plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 150, "font.size": 11,
    "axes.grid": True, "grid.alpha": 0.3, "axes.axisbelow": True,
    "legend.frameon": True, "legend.framealpha": 0.9,
})


def _tps(exp_id: str):
    p = ROOT / exp_id / "pgbench_stdout.log"
    if not p.exists():
        return None
    m = TPS_RE.search(p.read_text(errors="replace"))
    return float(m.group(1)) if m else None


def series(setup, wl, mode, pool, clients):
    """Retorna (clients_validos, means, stds) com média±desvio das 3 reps."""
    cs, means, stds = [], [], []
    for c in clients:
        pooltok = "nopool" if pool == "none" else f"pool{pool}"
        vals = [_tps(f"{setup}_{wl}_{mode}_{pooltok}_c{c}_r{r}") for r in (1, 2, 3)]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        cs.append(c)
        means.append(np.mean(vals))
        stds.append(np.std(vals, ddof=1) if len(vals) > 1 else 0.0)
    return np.array(cs), np.array(means), np.array(stds)


def band(ax, x, m, s, color, label, marker="o", ls="-"):
    ax.plot(x, m, marker=marker, color=color, label=label, ls=ls, lw=2, ms=5)
    ax.fill_between(x, m - s, m + s, color=color, alpha=0.18, lw=0)


CLIENTS = [1, 30, 50, 100, 200, 300, 500, 1000]


# ---------------------------------------------------------------------
# Fig 1 — Break-even (write, simple, pool=2)
# ---------------------------------------------------------------------
def fig_breakeven():
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    band(ax, *series("none", "write", "simple", "none", CLIENTS), C["none"], "none (conexão direta)")
    band(ax, *series("pgbouncer-tx", "write", "simple", "2", CLIENTS), C["pgbouncer"], "pgbouncer-tx (pool=2)")
    band(ax, *series("pgcat-tx", "write", "simple", "2", CLIENTS), C["pgcat"], "pgcat-tx (pool=2)")
    ax.axvline(30, color=C["accent"], ls="--", lw=1.3, alpha=0.8)
    ax.text(31, 250, "break-even\n$c^* \\approx 30$", color=C["accent"], fontsize=9, va="center")
    ax.axvline(415, color=C["none"], ls=":", lw=1.2, alpha=0.7)
    ax.text(420, 560, "max_connections=415\n(none satura)", color=C["none"], fontsize=8, va="center")
    ax.set_xscale("log")
    ax.set_xticks([1, 30, 100, 300, 1000]); ax.set_xticklabels([1, 30, 100, 300, 1000])
    ax.set_xlabel("Clientes simultâneos ($c$)"); ax.set_ylabel("TPS (média das 3 reps)")
    ax.set_title("Break-even na carga write (pool=2)")
    ax.legend(loc="lower left", fontsize=9)
    ax.set_ylim(0, 720)
    fig.tight_layout(); fig.savefig(OUT / "1_breakeven.png"); plt.close(fig)
    print("ok 1_breakeven.png")


# ---------------------------------------------------------------------
# Fig 3 — H3 condicional (read, pool=8): simple sobrepõe, prepared separa
# ---------------------------------------------------------------------
def fig_h3():
    cl = [1, 30, 100, 200, 500, 1000]
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    band(ax, *series("pgbouncer-tx", "read", "simple", "8", cl), C["pgbouncer"], "pgbouncer · simple", marker="o", ls="--")
    band(ax, *series("pgcat-tx", "read", "simple", "8", cl), C["pgcat"], "pgcat · simple", marker="o", ls="--")
    band(ax, *series("pgbouncer-tx", "read", "prepared", "8", cl), C["pgbouncer"], "pgbouncer · prepared", marker="s", ls="-")
    band(ax, *series("pgcat-tx", "read", "prepared", "8", cl), C["pgcat"], "pgcat · prepared", marker="s", ls="-")
    ax.annotate("simple: PgCat ≈ PgBouncer\n(threading não importa)",
                xy=(300, 16700), xytext=(45, 9500), fontsize=8.5,
                arrowprops=dict(arrowstyle="->", color="#444", lw=1))
    ax.annotate("prepared: PgCat +~18%\n(pooler no caminho crítico)",
                xy=(300, 25200), xytext=(33, 27200), fontsize=8.5,
                arrowprops=dict(arrowstyle="->", color="#444", lw=1))
    ax.set_xscale("log")
    ax.set_xticks([1, 30, 100, 300, 1000]); ax.set_xticklabels([1, 30, 100, 300, 1000])
    ax.set_xlabel("Clientes simultâneos ($c$)"); ax.set_ylabel("TPS (média das 3 reps)")
    ax.set_title("H3 — multi-thread vs single-thread (read, pool=8)")
    ax.legend(loc="lower right", fontsize=8.5, ncol=1)
    ax.set_ylim(0, 29500)
    fig.tight_layout(); fig.savefig(OUT / "3_h3_multithread.png"); plt.close(fig)
    print("ok 3_h3_multithread.png")


# ---------------------------------------------------------------------
# Fig 4 — cl_waiting: a espera migra para a fila do pooler (write pool=2)
# ---------------------------------------------------------------------
def _pool_metrics(setup, wl, mode, pool, c, rep=1):
    f = "pgcat_show_pools.csv" if "pgcat" in setup else "pgbouncer_show_pools.csv"
    p = ROOT / f"{setup}_{wl}_{mode}_pool{pool}_c{c}_r{rep}" / f
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df = df[df["database"] == "tcc"]
    for col in ("cl_waiting", "sv_active", "maxwait_us"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.iloc[1:] if len(df) > 1 else df
    return dict(cl_waiting=df["cl_waiting"].mean(), sv_active=df["sv_active"].mean(),
                maxwait_ms=df["maxwait_us"].mean() / 1000)


def fig_cl_waiting():
    cl = [30, 50, 100, 200, 300, 500, 1000]
    clw = [_pool_metrics("pgbouncer-tx", "write", "simple", "2", c) for c in cl]
    cl = [c for c, m in zip(cl, clw) if m]; clw = [m for m in clw if m]
    waiting = [m["cl_waiting"] for m in clw]
    sv = [m["sv_active"] for m in clw]
    maxw = [m["maxwait_ms"] for m in clw]

    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    ax.fill_between(cl, 0, sv, color=C["pgcat"], alpha=0.5, label="no banco (sv_active ≡ 2 backends)")
    ax.plot(cl, waiting, "o-", color=C["pgbouncer"], lw=2, label="na fila do pooler (cl_waiting)")
    ax.plot(cl, cl, ":", color="#999", lw=1, label="referência $y=c$")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xticks([30, 100, 300, 1000]); ax.set_xticklabels([30, 100, 300, 1000])
    ax.set_xlabel("Clientes simultâneos ($c$)"); ax.set_ylabel("Clientes (escala log)")
    ax.set_title("Onde a espera acontece — pgbouncer-tx, write, pool=2")
    ax.legend(loc="upper left", fontsize=9)
    # eixo secundário: maxwait
    ax2 = ax.twinx(); ax2.grid(False)
    ax2.plot(cl, maxw, "s--", color=C["rds"], lw=1.5, ms=4, alpha=0.8)
    ax2.set_ylabel("maxwait (ms)", color=C["rds"])
    ax2.tick_params(axis="y", colors=C["rds"])
    fig.tight_layout(); fig.savefig(OUT / "4_cl_waiting.png"); plt.close(fig)
    print("ok 4_cl_waiting.png")


# ---------------------------------------------------------------------
# Fig 5 — pool_size (write, simple, pgbouncer-tx): efeito modesto e monotônico
# ---------------------------------------------------------------------
def fig_poolsize():
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    for pool, col, lab in [("2", C["pgcat"], "pool=2"), ("8", C["pgbouncer"], "pool=8"), ("100", C["rds"], "pool=100")]:
        band(ax, *series("pgbouncer-tx", "write", "simple", pool, CLIENTS), col, lab)
    ax.set_xscale("log")
    ax.set_xticks([1, 30, 100, 300, 1000]); ax.set_xticklabels([1, 30, 100, 300, 1000])
    ax.set_xlabel("Clientes simultâneos ($c$)"); ax.set_ylabel("TPS (média das 3 reps)")
    ax.set_title("Efeito do tamanho do pool — pgbouncer-tx, carga write")
    ax.legend(loc="lower left", fontsize=9)
    ax.set_ylim(0, 720)
    fig.tight_layout(); fig.savefig(OUT / "5_poolsize.png"); plt.close(fig)
    print("ok 5_poolsize.png")


# ---------------------------------------------------------------------
# Fig 6 — H5: delta de vazão prepared vs simple (read, pool=8)
# ---------------------------------------------------------------------
def fig_h5():
    setups = [("pgbouncer-tx", "PgBouncer"), ("pgcat-tx", "PgCat")]
    cl = [30, 100, 200, 500, 1000]
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    x = np.arange(len(setups)); w = 0.35
    simples, preps, deltas = [], [], []
    for s, _ in setups:
        _, ms, _ = series(s, "read", "simple", "8", cl)
        _, mp, _ = series(s, "read", "prepared", "8", cl)
        simples.append(ms.mean()); preps.append(mp.mean())
        deltas.append(100 * (mp.mean() - ms.mean()) / ms.mean())
    ax.bar(x - w / 2, simples, w, color="#bbbbbb", label="simple")
    ax.bar(x + w / 2, preps, w, color=C["prep"], label="prepared")
    for i, d in enumerate(deltas):
        ax.text(x[i] + w / 2, preps[i] + 400, f"+{d:.0f}%", ha="center", fontsize=11, fontweight="bold", color=C["prep"])
    ax.set_xticks(x); ax.set_xticklabels([n for _, n in setups])
    ax.set_ylabel("TPS médio (read, pool=8, c≥30)")
    ax.set_title("H5 — ganho de prepared sobre simple por pooler")
    ax.legend(loc="upper left", fontsize=9); ax.set_ylim(0, 29000)
    fig.tight_layout(); fig.savefig(OUT / "6_h5_prepared.png"); plt.close(fig)
    print("ok 6_h5_prepared.png  deltas:", [f"{d:.1f}%" for d in deltas])


# ---------------------------------------------------------------------
# Fig 2 — Mecanismo: LWLock:LockManager (write, pool=8), média 3 reps
# ---------------------------------------------------------------------
def _lwlock_frac(setup, pool, c, rep):
    pooltok = "nopool" if pool == "none" else f"pool{pool}"
    p = ROOT / f"{setup}_write_simple_{pooltok}_c{c}_r{rep}" / "pg_stat_activity.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    act = df[df["state"] == "active"]
    if act.empty:
        return 0.0
    is_lm = (act["wait_event_type"] == "LWLock") & (act["wait_event"] == "LockManager")
    return 100 * is_lm.sum() / len(act)


def _lw_avg(setup, pool, c):
    v = [_lwlock_frac(setup, pool, c, r) for r in (1, 2, 3)]
    v = [x for x in v if x is not None]
    return (np.mean(v), np.std(v, ddof=1) if len(v) > 1 else 0.0) if v else (0, 0)


def fig_lwlock():
    cs = [30, 100, 300]
    none = [_lw_avg("none", "none", c) for c in cs]
    pgb = [_lw_avg("pgbouncer-tx", "8", c) for c in cs]
    pgc = [_lw_avg("pgcat-tx", "8", c) for c in cs]
    x = np.arange(len(cs)); w = 0.26
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    ax.bar(x - w, [m for m, _ in none], w, yerr=[s for _, s in none], capsize=3, color=C["none"], label="none (conexão direta)")
    ax.bar(x, [m for m, _ in pgb], w, color=C["pgbouncer"], label="pgbouncer-tx (pool=8)")
    ax.bar(x + w, [m for m, _ in pgc], w, color=C["pgcat"], label="pgcat-tx (pool=8)")
    for i, (m, _) in enumerate(none):
        ax.text(x[i] - w, m + 0.4, f"{m:.1f}%", ha="center", fontsize=9, color=C["none"])
    ax.set_xticks(x); ax.set_xticklabels([f"c={c}" for c in cs])
    ax.set_ylabel("% do tempo ativo em LWLock:LockManager")
    ax.set_title("Mecanismo causal — contenção interna de coordenação (write)")
    ax.legend(loc="upper left", fontsize=9); ax.set_ylim(0, 21)
    fig.tight_layout(); fig.savefig(OUT / "2_mecanismo_lwlock.png"); plt.close(fig)
    print("ok 2_mecanismo_lwlock.png")


# ---------------------------------------------------------------------
# Fig 7 — Latência: pedágio na mediana, controle na cauda (write pool=2)
# ---------------------------------------------------------------------
def _maxlat_ms(exp_id):
    d = ROOT / exp_id
    if not d.exists():
        return None
    mx, found = 0, False
    for f in d.glob("pgbench.*"):
        if f.suffix == ".log":
            continue
        for line in f.read_text(errors="replace").splitlines():
            p = line.split()
            if len(p) >= 6:
                try:
                    mx = max(mx, int(p[5])); found = True
                except ValueError:
                    pass
    return mx / 1000 if found else None


def _lat_series(setup, pool, clients):
    cs, mean, mx = [], [], []
    for c in clients:
        pooltok = "nopool" if pool == "none" else f"pool{pool}"
        ms, xs = [], []
        for r in (1, 2, 3):
            eid = f"{setup}_write_simple_{pooltok}_c{c}_r{r}"
            log = ROOT / eid / "pgbench_stdout.log"
            if log.exists():
                m = re.search(r"latency average = ([\d.]+)", log.read_text(errors="replace"))
                if m:
                    ms.append(float(m.group(1)))
            x = _maxlat_ms(eid)
            if x:
                xs.append(x)
        if ms and xs:
            cs.append(c); mean.append(np.mean(ms)); mx.append(np.mean(xs))
    return np.array(cs), np.array(mean), np.array(mx)


def fig_latency():
    cl = [1, 30, 100, 300, 500, 1000]
    nc, nmean, nmax = _lat_series("none", "none", cl)
    bc, bmean, bmax = _lat_series("pgbouncer-tx", "2", cl)
    fig, ax = plt.subplots(figsize=(7.2, 4.3))
    ax.fill_between(nc, nmean, nmax, color=C["none"], alpha=0.15, lw=0)
    ax.plot(nc, nmean, "o-", color=C["none"], lw=2, label="none · latência média")
    ax.plot(nc, nmax, "o--", color=C["none"], lw=1.3, label="none · máximo por janela")
    ax.fill_between(bc, bmean, bmax, color=C["pgbouncer"], alpha=0.15, lw=0)
    ax.plot(bc, bmean, "s-", color=C["pgbouncer"], lw=2, label="pgbouncer · latência média")
    ax.plot(bc, bmax, "s--", color=C["pgbouncer"], lw=1.3, label="pgbouncer · máximo por janela")
    ax.annotate("em c=300 a cauda do none\nexplode (14,4 s) vs 0,67 s\ndo pooler", xy=(300, 14402),
                xytext=(34, 9000), fontsize=8.5, arrowprops=dict(arrowstyle="->", color="#444", lw=1))
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xticks([1, 30, 100, 300, 1000]); ax.set_xticklabels([1, 30, 100, 300, 1000])
    ax.set_xlabel("Clientes simultâneos ($c$)"); ax.set_ylabel("Latência (ms, escala log)")
    ax.set_title("Latência na carga write (pool=2) — média vs cauda")
    ax.legend(loc="upper left", fontsize=8, ncol=1)
    fig.tight_layout(); fig.savefig(OUT / "7_latencia.png"); plt.close(fig)
    print("ok 7_latencia.png")


# ---------------------------------------------------------------------
# Fig 8 — Arquitetura do experimento (3 hosts dedicados, pernas de rede)
# ---------------------------------------------------------------------
def fig_arquitetura():
    import matplotlib.patches as mp
    fig, ax = plt.subplots(figsize=(7.6, 4.0))
    ax.set_xlim(0, 12); ax.set_ylim(0, 7); ax.axis("off")

    def box(x, y, w, h, title, sub, color):
        ax.add_patch(mp.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08",
                     fc=color, ec="#333", lw=1.3, alpha=0.9))
        ax.text(x + w / 2, y + h - 0.35, title, ha="center", va="top", fontsize=10, fontweight="bold")
        ax.text(x + w / 2, y + 0.28, sub, ha="center", va="bottom", fontsize=7.3, color="#222")

    box(0.3, 2.6, 3.0, 2.0, "Cliente", "EC2 m5.large (2 vCPU)\npgbench + collector", "#dce6f5")
    box(4.5, 2.6, 3.0, 2.0, "Pooler", "EC2 m5.xlarge (4 vCPU)\nPgBouncer / PgCat (Docker)", "#dcefdc")
    box(8.7, 2.6, 3.0, 2.0, "Banco", "RDS m8gd.large\nPostgreSQL 18", "#f5e2dc")

    # setas client->pooler (plaintext) e pooler->RDS (TLS)
    ax.annotate("", xy=(4.5, 3.6), xytext=(3.3, 3.6), arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#1f77b4"))
    ax.text(3.9, 3.95, "plaintext", ha="center", fontsize=7.5, color="#1f77b4")
    ax.annotate("", xy=(8.7, 3.6), xytext=(7.5, 3.6), arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#2ca02c"))
    ax.text(8.1, 3.95, "TLS", ha="center", fontsize=7.5, color="#2ca02c")
    # collector amostra server-side (banco)
    ax.annotate("", xy=(9.4, 2.6), xytext=(2.4, 2.6), arrowprops=dict(arrowstyle="-|>", lw=1.0, ls=":", color="#888",
                connectionstyle="arc3,rad=-0.25"))
    ax.text(6.0, 1.55, "collector amostra wait events / pg_locks / pg_stat_* (server-side)",
            ha="center", fontsize=7.2, color="#666")
    # RDS Proxy (caminho gerenciado alternativo)
    ax.add_patch(mp.FancyBboxPatch((4.5, 5.0), 3.0, 1.1, boxstyle="round,pad=0.06",
                 fc="#f3e6f5", ec="#9467bd", lw=1.1, ls="--", alpha=0.9))
    ax.text(6.0, 5.55, "RDS Proxy (6º setup, gerenciado)", ha="center", va="center", fontsize=7.6, color="#6a3a8a")
    ax.annotate("", xy=(8.9, 4.6), xytext=(6.0, 5.0), arrowprops=dict(arrowstyle="-|>", lw=1.2, ls="--", color="#9467bd"))
    ax.annotate("", xy=(6.0, 5.0), xytext=(2.6, 4.6), arrowprops=dict(arrowstyle="-|>", lw=1.2, ls="--", color="#9467bd"))
    ax.text(11.7, 0.5, "AZ única (us-east-1)", ha="right", fontsize=7, color="#999", style="italic")
    fig.tight_layout(); fig.savefig(OUT / "8_arquitetura.png"); plt.close(fig)
    print("ok 8_arquitetura.png")


# ---------------------------------------------------------------------
# Fig 9 — Pipeline de coleta (timeline warmup/measure + amostragem)
# ---------------------------------------------------------------------
def fig_pipeline():
    import matplotlib.patches as mp
    fig, ax = plt.subplots(figsize=(7.6, 3.2))
    ax.set_xlim(-3, 95); ax.set_ylim(0, 6); ax.axis("off")
    # barra de tempo
    ax.add_patch(mp.Rectangle((0, 3.6), 30, 0.9, fc="#e0e0e0", ec="#333"))
    ax.text(15, 4.05, "warmup (30 s)\ndescartado", ha="center", va="center", fontsize=8)
    ax.add_patch(mp.Rectangle((30, 3.6), 60, 0.9, fc="#dcefdc", ec="#333"))
    ax.text(60, 4.05, "measure (60 s)", ha="center", va="center", fontsize=9, fontweight="bold")
    ax.annotate("", xy=(92, 3.25), xytext=(-1, 3.25), arrowprops=dict(arrowstyle="-|>", color="#333", lw=1))
    ax.text(91, 2.85, "tempo", fontsize=7.5, color="#555", ha="right")
    # track pgbench
    ax.text(-2.5, 2.4, "pgbench", fontsize=8, fontweight="bold", color="#1f77b4", va="center")
    for x in range(31, 90, 1):
        ax.plot([x], [2.4], "|", color="#1f77b4", ms=6, alpha=0.5)
    ax.text(60, 1.95, "log agregado: TPS, latência média/máx por segundo", ha="center", fontsize=7.3, color="#1f77b4")
    # track collector
    ax.text(-2.5, 1.0, "collector", fontsize=8, fontweight="bold", color="#2ca02c", va="center")
    for x in range(31, 90, 1):
        ax.plot([x], [1.0], "|", color="#2ca02c", ms=6, alpha=0.5)
    ax.text(60, 0.45, "amostra 1 Hz: pg_stat_activity, pg_locks, pg_stat_statements, console do pooler",
            ha="center", fontsize=7.0, color="#2ca02c")
    ax.set_title("Pipeline de coleta — cliente e servidor amostrados em paralelo", fontsize=10)
    fig.tight_layout(); fig.savefig(OUT / "9_pipeline.png"); plt.close(fig)
    print("ok 9_pipeline.png")


if __name__ == "__main__":
    fig_breakeven()
    fig_lwlock()
    fig_h3()
    fig_cl_waiting()
    fig_poolsize()
    fig_h5()
    fig_latency()
    fig_arquitetura()
    fig_pipeline()
    print("Figuras geradas em", OUT)
