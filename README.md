# Bot Telegram de gestion de groupe

Bot autonome, prêt à être déployé sur Railway. Il fonctionne sans bibliothèque Python externe et utilise directement l’API officielle Telegram.

## Fonctionnement inclus

- Ouverture automatique quotidienne, par défaut de **23 h à 2 h** (`Europe/Paris`).
- Le bouton automatique peut être mis sur `OFF` à tout moment, y compris pendant une séance : le groupe est verrouillé avant la publication du message d’annulation.
- Fermeture renforcée : les permissions Telegram sont réappliquées toutes les 60 secondes et tout message non administrateur reçu hors horaires est supprimé.
- Suppression automatique des messages de service indiquant qu’un membre est entré dans le groupe ou l’a quitté.
- Pendant l’ouverture, seuls les **messages texte, photos et vidéos** sont conservés. Les autres contenus sont supprimés.
- Compte à rebours envoyé chaque heure, puis toutes les 15 minutes pendant la dernière heure. Le message précédent est supprimé.
- Avertissements uniques à 30, 15 et 5 minutes de la fermeture.
- Panneau administrateur entièrement composé de boutons, accessible avec `/panel` dans la conversation privée du bot.
- Activation/désactivation de l’ouverture automatique, des liens interdits et des transferts interdits.
- Lien interdit : suppression et bannissement immédiat de l’auteur.
- Message transféré interdit : suppression sans sanction.
- Story partagée : suppression et bannissement immédiat.
- Mots interdits configurables : 1 jour de restriction, 3 jours en cas de récidive, puis bannissement.
- Règles configurables, publiées trois fois pendant chaque séance. Avec l’horaire 23 h–2 h, elles sont publiées vers 23 h, 0 h et 1 h.
- Horaires disponibles dans le panneau : 22 h–0 h, 23 h–1 h, 23 h–2 h et 0 h–3 h.
- Données persistantes dans SQLite : options, horaires, règles, mots interdits, récidives et rappels déjà envoyés.
- Point de contrôle Railway : `GET /health`.

## 1. Créer et préparer le bot Telegram

1. Ouvrez [@BotFather](https://t.me/BotFather), lancez `/newbot` et conservez le jeton secret.
2. Dans BotFather, désactivez le mode confidentialité du bot avec `/setprivacy`. Cela permet au bot de recevoir tous les messages à modérer.
3. Ajoutez le bot au groupe, puis nommez-le administrateur.
4. Accordez-lui au minimum les droits suivants :
   - supprimer les messages ;
   - bannir et restreindre les membres.
5. Utilisez un **supergroupe** : les restrictions temporaires individuelles nécessitent ce type de groupe.

Les personnes déclarées dans `ADMIN_IDS` peuvent utiliser le panneau et sont exemptées de la modération. Pour qu’elles puissent aussi écrire pendant la fermeture, nommez-les administrateurs du groupe Telegram : les permissions par défaut d’un groupe fermé bloquent les membres ordinaires.

## 2. Trouver les identifiants

### Identifiant du groupe

Après avoir ajouté le bot au groupe, envoyez un message dans le groupe, puis interrogez localement l’API officielle :

```bash
export BOT_TOKEN='votre_token_secret'
curl "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
```

Recherchez `message.chat.id`. Pour un supergroupe, la valeur ressemble à `-1001234567890`.

### Identifiants des administrateurs du panneau

Utilisez les identifiants numériques personnels Telegram et placez-les dans `ADMIN_IDS`, séparés par des virgules. Ne mettez jamais de nom d’utilisateur `@pseudo` dans cette variable.

## 3. Déployer sur Railway

1. Placez ce dossier dans un dépôt GitHub privé.
2. Dans [Railway](https://railway.com/), créez un projet avec **Deploy from GitHub repo** et sélectionnez le dépôt.
3. Ajoutez les variables suivantes dans l’onglet **Variables** :

| Variable | Exemple | Obligatoire |
| --- | --- | --- |
| `BOT_TOKEN` | `123456789:secret` | Oui |
| `TARGET_CHAT_ID` | `-1001234567890` | Oui |
| `ADMIN_IDS` | `123456789,987654321` | Oui |
| `GROUP_INVITE_LINK` | `https://t.me/+...` | Recommandé |
| `TZ` | `Europe/Paris` | Non, valeur par défaut |
| `DATABASE_PATH` | `/data/bot.sqlite3` | Non, valeur par défaut |
| `LOG_LEVEL` | `INFO` | Non |

4. Dans le service Railway, ajoutez un **Volume** monté sur `/data`. Sans volume, les réglages et les récidives peuvent être perdus lors d’un redéploiement.
5. Gardez une seule réplique du service : un bot utilisant `getUpdates` ne doit pas avoir plusieurs processus de long polling actifs avec le même jeton.
6. Déployez. Le fichier `railway.json` sélectionne automatiquement le `Dockerfile`, démarre `python main.py` et utilise `/health` pour vérifier le service.

Railway documente les volumes persistants ici : [Railway Volumes](https://docs.railway.com/volumes/reference).

## 4. Utiliser le panneau

1. Ouvrez la conversation privée avec le bot.
2. Envoyez `/start`, puis `/panel`.
3. Utilisez les boutons :
   - `Automatique ON/OFF` ;
   - `Liens ON/OFF` ;
   - `Forwards ON/OFF` ;
   - `Mots interdits` → Voir, Ajouter ou Supprimer ;
   - `Règles` → Voir/Modifier ou Publier maintenant ;
   - `Horaires` → choisir un créneau ;
   - `Resynchroniser` → réappliquer immédiatement les permissions et actualiser les administrateurs Telegram.

Quand l’ouverture automatique passe sur `OFF`, le groupe est fermé immédiatement et le bot publie :

> Aucune ouverture n’est prévue aujourd’hui. Revenez demain et partagez le groupe :  
> lien principal du groupe

Tous les nouveaux messages, photos ou vidéos envoyés par des membres ordinaires après ce passage sur `OFF` sont supprimés. L’API Telegram ne permet pas à un bot de relire librement tout l’historique du groupe : cette suppression concerne donc les contenus reçus par le bot au moment de la fermeture ou après celle-ci, et non d’anciens médias déjà présents avant son démarrage.

## Sécurité et comportement en cas de panne

- Le jeton n’est jamais écrit dans les logs. Conservez `BOT_TOKEN` uniquement dans les variables Railway.
- Au démarrage, le bot supprime un ancien webhook et abandonne les mises à jour devenues obsolètes, puis resynchronise immédiatement l’état du groupe.
- Si une personne ouvre manuellement le groupe hors horaires, le bot le referme au plus tard lors de la prochaine vérification (60 secondes). Les messages reçus entre-temps sont aussi supprimés.
- Si Telegram ne répond pas pendant la vérification du statut d’un membre, le contenu interdit est supprimé par sécurité, mais aucun bannissement ni restriction irréversible n’est appliqué sans confirmation.
- Si le processus redémarre pendant une séance, les rappels déjà enregistrés ne sont pas renvoyés en double.
- Le bot vérifie une dernière fois qu’un utilisateur n’est pas administrateur Telegram avant toute sanction irréversible.

## Développement local

Copiez les variables de `.env.example` dans votre environnement, puis lancez :

```bash
python main.py
```

Exécutez les tests :

```bash
python -m unittest discover -v
```

La suite teste notamment les **1 024 combinaisons** des règles de modération, les quatre créneaux horaires, le passage à minuit, le bouton `OFF` pendant une séance, les échecs temporaires de Telegram et la suppression des messages d’entrée/sortie.

Le projet est volontairement sans dépendance externe : Python 3.12 suffit.

## Référence Telegram

Le verrouillage utilise `setChatPermissions`, les sanctions utilisent `restrictChatMember` et `banChatMember`, et les stories partagées sont identifiées avec le champ `Message.story` de l’[API officielle Telegram Bot](https://core.telegram.org/bots/api).
