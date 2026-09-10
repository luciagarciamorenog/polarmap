"""
Batería de casos conocidos. Si el índice no pasa esto, no mide polarización.
"""
import numpy as np
from metrics import (
    esteban_ray, bimodality_coefficient, hartigan_dip,
    dispersion, polarization_profile, affective_polarization,
    effective_sample_size,
)

rng = np.random.default_rng(42)
N = 5000


def clip(x):
    return np.clip(x, -1, 1)


CASES = {
    # nombre: (generador, expectativa)
    "consenso_total":        clip(rng.normal(0.0, 0.05, N)),
    "consenso_desplazado":   clip(rng.normal(0.5, 0.05, N)),
    "campana_ancha":         clip(rng.normal(0.0, 0.35, N)),
    "uniforme":              rng.uniform(-1, 1, N),
    "bimodal_suave":         clip(np.concatenate([rng.normal(-0.4, 0.2, N//2),
                                                  rng.normal(0.4, 0.2, N//2)])),
    "bimodal_extremo":       clip(np.concatenate([rng.normal(-0.85, 0.1, N//2),
                                                  rng.normal(0.85, 0.1, N//2)])),
    "bimodal_asimetrico":    clip(np.concatenate([rng.normal(-0.85, 0.1, int(N*0.8)),
                                                  rng.normal(0.85, 0.1, int(N*0.2))])),
    "trimodal":              clip(np.concatenate([rng.normal(-0.8, 0.1, N//3),
                                                  rng.normal(0.0, 0.1, N//3),
                                                  rng.normal(0.8, 0.1, N//3)])),
}

print(f"{'caso':<22} {'ER':>7} {'disp':>7} {'BC':>7} {'dip_p':>8}  multimodal")
print("-" * 68)
results = {}
for name, data in CASES.items():
    er = esteban_ray(data)
    d = dispersion(data)
    bc = bimodality_coefficient(data)
    dip = hartigan_dip(data)
    results[name] = er
    print(f"{name:<22} {er:>7.3f} {d:>7.3f} {bc:>7.3f} {dip['p_value']:>8.4f}  {dip['is_multimodal']}")

print()
print("=== ASERCIONES CRÍTICAS ===")

checks = []

# 1. Consenso -> polarización ~0
checks.append(("consenso da ER bajo",
               results["consenso_total"] < 0.05))

# 2. Un consenso desplazado NO es polarización (error clásico: confundir
#    "opinión extrema mayoritaria" con "sociedad dividida")
checks.append(("consenso desplazado NO es polarizacion",
               results["consenso_desplazado"] < 0.05))

# 3. LA CLAVE: uniforme tiene dispersión alta pero polarización baja
disp_unif = dispersion(CASES["uniforme"])
disp_bimod = dispersion(CASES["bimodal_extremo"])
checks.append((f"uniforme (disp={disp_unif:.2f}) < bimodal (disp={disp_bimod:.2f}) en ER",
               results["uniforme"] < results["bimodal_extremo"]))

# 4. Bimodal extremo > bimodal suave
checks.append(("bimodal extremo > bimodal suave",
               results["bimodal_extremo"] > results["bimodal_suave"]))

# 5. Bimodal simétrico > asimétrico (80/20 está menos polarizado: hay
#    un grupo dominante, no dos bloques enfrentados de igual fuerza)
checks.append(("bimodal simetrico > asimetrico 80/20",
               results["bimodal_extremo"] > results["bimodal_asimetrico"]))

# 6. Trimodal < bimodal (existir un centro reduce la polarización)
checks.append(("trimodal < bimodal (el centro amortigua)",
               results["trimodal"] < results["bimodal_extremo"]))

# 7. Campana ancha no dispara falsos positivos
checks.append(("campana ancha no es multimodal (dip)",
               hartigan_dip(CASES["campana_ancha"])["is_multimodal"] is False))

# 8. Bimodal SÍ se detecta como multimodal
checks.append(("bimodal detectado como multimodal (dip)",
               hartigan_dip(CASES["bimodal_extremo"])["is_multimodal"] is True))

for desc, ok in checks:
    print(f"  [{'PASA' if ok else 'FALLA'}] {desc}")

print()
print("=== POLARIZACION AFECTIVA ===")
# Distribucion CON centro poblado, para poder comparar extremos vs centro
pos = clip(np.concatenate([rng.normal(-0.75, 0.15, 2000),
                           rng.normal(0.0, 0.15, 1000),
                           rng.normal(0.75, 0.15, 2000)]))

# escenario A: hostilidad concentrada en extremos (polarización afectiva real)
host_polar = np.clip(0.15 + 0.7 * np.abs(pos) + rng.normal(0, 0.05, pos.size), 0, 1)
# escenario B: hostilidad plana y alta (crispación general, no polarización)
host_flat = np.clip(rng.normal(0.6, 0.05, pos.size), 0, 1)
# escenario C: debate duro pero civilizado
host_civil = np.clip(rng.normal(0.1, 0.03, pos.size), 0, 1)

aff_scores = {}
for label, h in [("hostilidad en extremos", host_polar),
                 ("hostilidad plana alta", host_flat),
                 ("debate civilizado", host_civil)]:
    a = affective_polarization(pos, h)
    aff_scores[label] = a["affective_index"]
    print(f"  {label:<24} afectivo={a['affective_index']:.3f}  "
          f"ratio_ext/centro={a['extremity_ratio']:.2f}  centro={a['center_share']:.2f}")

# Centro vacio -> ratio infinito, no nan
a_empty = affective_polarization(CASES["bimodal_extremo"], 
                                 np.clip(rng.normal(0.7, 0.05, N), 0, 1))
print(f"  {'centro vacio':<24} afectivo={a_empty['affective_index']:.3f}  "
      f"ratio={a_empty['extremity_ratio']}  centro={a_empty['center_share']:.2f}")

print()
print("=== ASERCIONES FASE 2 ===")
checks2 = [
    ("rango dinamico util (bimodal extremo > 0.60)",
     results["bimodal_extremo"] > 0.60),
    ("consenso sigue bajo (<0.10)",
     results["consenso_total"] < 0.10),
    ("hostilidad en extremos > hostilidad plana",
     aff_scores["hostilidad en extremos"] > aff_scores["hostilidad plana alta"]),
    ("debate civilizado es el mas bajo",
     aff_scores["debate civilizado"] < min(aff_scores["hostilidad en extremos"],
                                           aff_scores["hostilidad plana alta"])),
    ("centro vacio NO produce nan",
     np.isfinite(a_empty["affective_index"])),
]
for desc, ok in checks2:
    print(f"  [{'PASA' if ok else 'FALLA'}] {desc}")
checks.extend(checks2)

print()
print("=== N EFECTIVO (deteccion de astroturfing) ===")
w_organic = rng.lognormal(0, 0.5, 10000)
w_botted = np.concatenate([rng.lognormal(0, 0.5, 9995), np.array([50000.]*5)])
print(f"  10.000 docs organicos      -> N_eff = {effective_sample_size(w_organic):>8.0f}")
print(f"  10.000 docs, 5 cuentas 90% -> N_eff = {effective_sample_size(w_botted):>8.0f}")

print()
print("=== PERFIL COMPLETO (ejemplo) ===")
prof = polarization_profile(pos, host_polar)
for k, v in prof.items():
    print(f"  {k:<20} {v}")

failed = [d for d, ok in checks if not ok]
print()
print("RESULTADO:", "TODAS LAS ASERCIONES PASAN" if not failed else f"FALLOS: {failed}")
