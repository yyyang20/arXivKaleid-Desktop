# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""arXiv Physics canonical 目录；查询选择与界面共享同一身份。"""
from dataclasses import dataclass

TAXONOMY_VERSION = "physics_categories_v1"
TAXONOMY_SOURCE = "https://arxiv.org/category_taxonomy"
DEFAULT_CATEGORIES = ("gr-qc", "astro-ph.HE", "astro-ph.GA")


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    archive: str


# 2026-10-13 对照官方 Physics 大类核验；仅列具体 Category，不包含父组或 alias。
ARCHIVES = (
    ("astro-ph", "天体物理学", "Astrophysics"),
    ("cond-mat", "凝聚态物理", "Condensed Matter"),
    ("gr-qc", "广义相对论与量子宇宙学", "General Relativity and Quantum Cosmology"),
    ("hep-ex", "高能物理实验", "High Energy Physics - Experiment"),
    ("hep-lat", "高能物理格点", "High Energy Physics - Lattice"),
    ("hep-ph", "高能物理唯象", "High Energy Physics - Phenomenology"),
    ("hep-th", "高能物理理论", "High Energy Physics - Theory"),
    ("math-ph", "数学物理", "Mathematical Physics"),
    ("nlin", "非线性科学", "Nonlinear Sciences"),
    ("nucl-ex", "核物理实验", "Nuclear Experiment"),
    ("nucl-th", "核物理理论", "Nuclear Theory"),
    ("physics", "物理学", "Physics"),
    ("quant-ph", "量子物理", "Quantum Physics"),
)
_ROWS = (
    ("astro-ph.CO", "Cosmology and Nongalactic Astrophysics"),
    ("astro-ph.EP", "Earth and Planetary Astrophysics"),
    ("astro-ph.GA", "Astrophysics of Galaxies"),
    ("astro-ph.HE", "High Energy Astrophysical Phenomena"),
    ("astro-ph.IM", "Instrumentation and Methods for Astrophysics"),
    ("astro-ph.SR", "Solar and Stellar Astrophysics"),
    ("cond-mat.dis-nn", "Disordered Systems and Neural Networks"),
    ("cond-mat.mes-hall", "Mesoscale and Nanoscale Physics"),
    ("cond-mat.mtrl-sci", "Materials Science"),
    ("cond-mat.other", "Other Condensed Matter"),
    ("cond-mat.quant-gas", "Quantum Gases"),
    ("cond-mat.soft", "Soft Condensed Matter"),
    ("cond-mat.stat-mech", "Statistical Mechanics"),
    ("cond-mat.str-el", "Strongly Correlated Electrons"),
    ("cond-mat.supr-con", "Superconductivity"),
    ("gr-qc", "General Relativity and Quantum Cosmology"),
    ("hep-ex", "High Energy Physics - Experiment"),
    ("hep-lat", "High Energy Physics - Lattice"),
    ("hep-ph", "High Energy Physics - Phenomenology"),
    ("hep-th", "High Energy Physics - Theory"),
    ("math-ph", "Mathematical Physics"),
    ("nlin.AO", "Adaptation and Self-Organizing Systems"),
    ("nlin.CD", "Chaotic Dynamics"),
    ("nlin.CG", "Cellular Automata and Lattice Gases"),
    ("nlin.PS", "Pattern Formation and Solitons"),
    ("nlin.SI", "Exactly Solvable and Integrable Systems"),
    ("nucl-ex", "Nuclear Experiment"),
    ("nucl-th", "Nuclear Theory"),
    ("physics.acc-ph", "Accelerator Physics"),
    ("physics.ao-ph", "Atmospheric and Oceanic Physics"),
    ("physics.app-ph", "Applied Physics"),
    ("physics.atm-clus", "Atomic and Molecular Clusters"),
    ("physics.atom-ph", "Atomic Physics"),
    ("physics.bio-ph", "Biological Physics"),
    ("physics.chem-ph", "Chemical Physics"),
    ("physics.class-ph", "Classical Physics"),
    ("physics.comp-ph", "Computational Physics"),
    ("physics.data-an", "Data Analysis, Statistics and Probability"),
    ("physics.ed-ph", "Physics Education"),
    ("physics.flu-dyn", "Fluid Dynamics"),
    ("physics.gen-ph", "General Physics"),
    ("physics.geo-ph", "Geophysics"),
    ("physics.hist-ph", "History and Philosophy of Physics"),
    ("physics.ins-det", "Instrumentation and Detectors"),
    ("physics.med-ph", "Medical Physics"),
    ("physics.optics", "Optics"),
    ("physics.plasm-ph", "Plasma Physics"),
    ("physics.pop-ph", "Popular Physics"),
    ("physics.soc-ph", "Physics and Society"),
    ("physics.space-ph", "Space Physics"),
    ("quant-ph", "Quantum Physics"),
)
PHYSICS_CATEGORIES = tuple(Category(identity, name, identity.split(".")[0]) for identity, name in _ROWS)
PHYSICS_IDS = frozenset(category.id for category in PHYSICS_CATEGORIES)


def validate_categories(value) -> tuple[str, ...]:
    """拒绝修补非法输入；复制选择顺序，分类匹配仍使用集合并集。"""
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 3:
        raise ValueError("category_selection_invalid")
    if any(not isinstance(c, str) or c not in PHYSICS_IDS for c in value):
        raise ValueError("category_selection_invalid")
    if len(set(value)) != len(value):
        raise ValueError("category_selection_invalid")
    return tuple(value)
