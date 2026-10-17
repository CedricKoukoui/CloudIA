"""Test de la cohérence du modèle quand on duplique les pods (v6). Sans cluster ni registre :
    python test_model_consistency.py

Il rejoue le piège de la v5 et vérifie la correction :
  1. v5 : chaque pod tire ses propres données au hasard et s'entraîne seul -> deux pods peuvent rendre
     des verdicts différents pour le même message (le test le MESURE) ;
  2. v6 : l'entraînement centralisé est reproductible -> un même modèle, des verdicts identiques ;
  3. la porte de qualité accepte le modèle normal et refuse un modèle trop sensible.
"""
import sys

import numpy as np
from sklearn.ensemble import IsolationForest

import model_lib

failures = []


def check(cond, message):
    print(("  OK     " if cond else "  ÉCHEC  ") + message)
    if not cond:
        failures.append(message)


# Messages « limites » : autour de la frontière entre trafic normal et anomalie, là où deux modèles
# légèrement différents divergent. Les anomalies franches du générateur ne suffiraient pas à le montrer.
rng = np.random.default_rng(2024)
borderline = rng.normal(loc=model_lib.NORMAL_MEAN, scale=[25, 1.5, 5], size=(20000, 3))


def v5_pod(pod_number):
    """Reproduit app.py de la v5 : données tirées par le pod lui-même (ici une graine par pod pour que le
    test soit déterministe ; en v5 il n'y avait pas de graine du tout), même random_state=42."""
    data = np.random.default_rng(1000 + pod_number).normal(loc=[100, 1, 20], scale=[10, 0.5, 2], size=(500, 3))
    return IsolationForest(contamination=0.05, random_state=42).fit(data)


print("== v5 : 10 pods, 10 entraînements locaux")
pods = [v5_pod(i) for i in range(10)]
verdicts = np.array([p.predict(borderline) for p in pods])
disagree = np.mean(verdicts.min(axis=0) != verdicts.max(axis=0))
pairwise = np.mean(verdicts[0] != verdicts[1])
print(f"         pod 1 et pod 2 se contredisent sur {pairwise:.1%} des messages limites ;")
print(f"         au moins deux des 10 pods se contredisent sur {disagree:.1%} d'entre eux.")
check(disagree > 0, "le piège existe : des pods dupliqués donnent des verdicts différents pour le même message")

print("== v6 : un entraînement centralisé, reproductible, chargé par tous les pods")
a = model_lib.train_model(model_lib.make_training_data(seed=42), seed=42)
b = model_lib.train_model(model_lib.make_training_data(seed=42), seed=42)
check(np.array_equal(a.predict(borderline), b.predict(borderline)),
      "même graine -> verdicts identiques sur tous les messages limites")
check(np.array_equal(a.decision_function(borderline), b.decision_function(borderline)),
      "même graine -> scores d'anomalie identiques au bit près")

print("== Porte de qualité")
m = model_lib.evaluate(a)
print(f"         modèle normal : détection {m['detection_rate']:.1%}, faux positifs {m['false_positive_rate']:.1%}")
check(model_lib.passes_quality_gate(m), "le modèle normal (contamination 0.05) est accepté")
bad = model_lib.train_model(model_lib.make_training_data(seed=42), seed=42, contamination=0.3)
mb = model_lib.evaluate(bad)
print(f"         modèle trop sensible : détection {mb['detection_rate']:.1%}, faux positifs {mb['false_positive_rate']:.1%}")
check(not model_lib.passes_quality_gate(mb), "un modèle trop sensible (contamination 0.3) est refusé")

print()
if failures:
    print(f"{len(failures)} contrôle(s) en échec.")
    sys.exit(1)
print("Cohérence du modèle vérifiée.")
