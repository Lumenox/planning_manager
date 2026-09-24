*Description du script python*
Pour chaque médecin :

Si quota_astreinte_j ≠ 0 :
total_astreinte_pondere = (total_astreinte × 2 + total_astreinte_ferie) / quota_astreinte_j
Si quota_garde ≠ 0 :
total_eq_ven = vendredi + veille_ferie
total_eq_ven_pondere = total_eq_ven / quota_garde
total_eq_sam = samedi + veille_lundi_ferie
total_eq_sam_pondere = total_eq_sam / quota_garde
total_garde = total_eq_ven_pondere + total_eq_sam_pondere
grand_total = total_garde + total_astreinte_pondere

Le principe : ces pondérés sont des ratios brut / quota. À chaque créneau, choix_med choisit le médecin éligible dont le ratio est le plus bas — donc plus le quota d'un médecin est élevé, plus il est censé absorber de gardes/astreintes avant de « rattraper » les autres. C'est typiquement une fraction d'ETP (équivalent temps plein) ou un multiplicateur métier que tu fixes toi-même (0 = ne fait pas ce type de créneau, 1 = base pleine, 0,5 = mi-temps, etc.)
