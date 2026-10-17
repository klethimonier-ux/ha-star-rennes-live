# STAR Rennes Live

Intégration Home Assistant pour le réseau **STAR** de Rennes : métro en temps réel, bus, vélos en libre-service et parcs relais, avec deux cartes Lovelace prêtes à l'emploi.

[![Version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fklethimonier-ux%2Fha-star-rennes-live%2Fmain%2Fcustom_components%2Fstar_rennes_live%2Fmanifest.json&query=%24.version&label=version&prefix=v&color=blue)](https://github.com/klethimonier-ux/ha-star-rennes-live/releases/latest)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validation](https://github.com/klethimonier-ux/ha-star-rennes-live/actions/workflows/validate.yml/badge.svg)](https://github.com/klethimonier-ux/ha-star-rennes-live/actions/workflows/validate.yml)
[![Auteur](https://img.shields.io/badge/auteur-klethimonier--ux-181717?logo=github)](https://github.com/klethimonier-ux)

## Fonctionnalités

**Métro (lignes a et b)**
- État des lignes et perturbations : ligne en panne, station fermée pendant le service. Elles s'affichent d'elles-mêmes en tête de la carte « Mes trajets » pour les lignes suivies (cochées, ou desservant une station suivie), y compris les stations fermées que vous ne suivez pas.
- Stations suivies : ouverte / fermée, ascenseurs et escaliers par direction, **deux prochains passages en temps réel**, avec l'**affluence** de chaque rame quand STAR la publie.
- Schéma des deux lignes avec la **position des rames en temps réel** (reconstituée à partir des prochains passages publiés par STAR, horaires GTFS en secours).

**Bus** : prochains départs d'un arrêt dans une direction, temps réel ou horaire théorique, et **alertes trafic** de la ligne : une ligne à l'arrêt (manifestation, grève…) est signalée en tête de carte, un report d'arrêt ou une déviation s'affiche dans l'encart du sens concerné.

**Le vélo STAR** : vélos et places disponibles par station, état (ouverte, en panne, maintenance, fermée).

**Parcs relais** : places libres, capacité, taux d'occupation.

## Installation

### Avec HACS (dépôt personnalisé)

1. HACS → menu ⋮ → **Dépôts personnalisés**.
2. Dépôt : `https://github.com/klethimonier-ux/ha-star-rennes-live`, type : **Intégration**.
3. Cherchez **STAR Rennes Live**, installez, puis redémarrez Home Assistant.

### Manuelle

Copiez `custom_components/star_rennes_live` dans le dossier `custom_components` de votre configuration, puis redémarrez Home Assistant.

> Cette intégration n'a rien à voir avec l'intégration « Star Rennes » (domaine `star_rennes`, bus uniquement) : les deux peuvent coexister.

## Clé d'API STAR

Les données viennent du portail open data [data.explore.star.fr](https://data.explore.star.fr). Sans clé, les appels passent par le quota anonyme, partagé entre tous les utilisateurs et souvent épuisé. **Créez votre propre clé** (gratuite) :

1. Créez un compte sur data.explore.star.fr et connectez-vous.
2. Dans votre compte, ouvrez la section des **clés d'API** et générez une clé.
3. Collez-la lors de la configuration de l'intégration (modifiable ensuite dans les options).

La clé est envoyée dans l'en-tête HTTP, jamais dans l'URL ni dans les journaux.

## Configuration

**Paramètres → Appareils et services → Ajouter une intégration → STAR Rennes Live**, puis :

1. Cochez les services à suivre (métro, bus, vélos, parcs relais) et saisissez votre clé.
2. Bus : choisissez la ligne, l'arrêt puis la direction (vous pourrez en ajouter d'autres dans les options).
3. Choisissez les stations de vélo, parcs relais, lignes et stations de métro.
4. **Schéma des rames en temps réel** : à cocher pour la carte « Métro ». Les horaires GTFS du métro (~8 Mo) sont alors téléchargés au démarrage puis une fois par jour.

Tout se modifie ensuite via **Configurer** sur l'intégration ; elle se recharge aussitôt.

## Cartes

Les deux cartes sont chargées automatiquement par l'intégration : aucune ressource à ajouter.

### « Mes trajets »

```yaml
type: custom:star-trajets-card
title: Mes trajets                         # facultatif
perturbations: false                       # facultatif : masque les alertes métro (affichées par défaut)
show_distributeurs: false                  # facultatif
entities:
  - sensor.becquerel_za_saint_sulpice      # bus
  - sensor.station_italie                  # station de métro
  - sensor.mairie_opera                    # station vélo
  - sensor.parc_relais_henri_freville      # parc relais
  - entity: sensor.atalante_grand_quartier
    name: Atalante                         # nom affiché (facultatif)
```

Les pannes de ligne et les stations fermées n'ont pas à être ajoutées : elles apparaissent seules en tête de carte, et une station dont la ligne est en panne affiche « ligne à l'arrêt » au lieu des prochains passages.

Remplacez les entités par les vôtres (Outils de développement → États). Pour une carte différente par personne, créez une carte par personne et utilisez l'onglet **Visibilité** de chaque carte.

### « Métro »

```yaml
type: custom:star-metro-card
title: Métro            # facultatif
lignes: [A, B]          # facultatif : ordre / filtre
quais_uniques: [JFK]    # terminus à quai unique (J.F. Kennedy, travaux du 2e quai en cours)
```

Astuce : pour l'afficher en grand, créez une vue de type **Panneau (une seule carte)**.

## Données et rafraîchissement

| Donnée | Fréquence |
|---|---|
| État des lignes et stations de métro | 30 s |
| Prochains passages et rames (temps réel) | 30 s |
| Bus | 60 s par arrêt suivi |
| Parcs relais | 2 min |
| Vélos | 4 min |
| Horaires GTFS du métro | 1 fois par jour (si le schéma est activé) |

Seuls les services sélectionnés sont interrogés.

## Limites connues

- La position des rames est **estimée** : STAR publie les deux prochains passages à chaque quai, pas la position GPS des rames.
- L'affluence n'est affichée que lorsque STAR la mesure (valeurs observées : « Vert Clair », « Vert Foncé » ; une valeur inconnue s'affiche en gris).

## Crédits

Données : open data STAR / Rennes Métropole ([data.explore.star.fr](https://data.explore.star.fr)), selon la licence indiquée sur le portail. Projet indépendant, non affilié à Keolis Rennes ni à Rennes Métropole.

Licence du code : [MIT](LICENSE).
