# Bot Telegram de gestion de groupe

Bot autonome, prêt à être déployé sur Railway. Il utilise directement l’API officielle Telegram et conserve sa configuration dans PostgreSQL.

## Fonctionnement inclus

- Ouverture automatique quotidienne, par défaut de **23 h à 2 h** (`Europe/Paris`).
- Le bouton automatique peut être mis sur `OFF` à tout moment, y compris pendant une séance : le groupe est verrouillé avant la publication du message d’annulation.
- Fermeture renforcée : les permissions Telegram sont réappliquées toutes les 60 secondes et tout message non administrateur reçu hors horaires est supprimé.
- Nettoyage complet à la fermeture : tous les messages reçus pendant la séance, y compris ceux des administrateurs et les annonces du bot, sont enregistrés puis supprimés par lots de 100.
- Suppression automatique des messages de service indiquant qu’un membre est entré dans le groupe ou l’a quitté.
- Pendant l’ouverture, seuls les **messages texte, photos et vidéos** sont conservés. Les autres contenus sont supprimés.
- Compte à rebours envoyé chaque heure, puis toutes les 15 minutes pendant la dernière heure. Le message précédent est supprimé.
- Avertissements uniques à 30, 15 et 5 minutes de la fermeture.
- Panneau administrateur entièrement composé de boutons, accessible avec `/panel` dans la conversation privée du bot.
- Activation/désactivation de l’ouverture automatique, des liens interdits et des transferts interdits.
- `TRUSTED_IDS` configurables : exemption limitée aux mots interdits et commandes de modération par réponse.
- Justice populaire activable/désactivable avec seuil configurable, fixé à **5 comptes distincts** par défaut.
- Publicité d’invitation unique (texte et photo), modifiable et publiable depuis le panneau.
- Parrainage par lien personnel avec demandes d’adhésion, compteur persistant et récompense automatique à **10 invitations validées**.
- Chaque lien personnel est associé à `TARGET_CHAT_ID` : après un changement de groupe, un ancien lien n’est jamais réutilisé.
- Renouvellement automatique des liens devenus obsolètes et bouton administrateur **Nouveaux liens pour tous** avec envoi privé progressif.
- Lien interdit : suppression et bannissement immédiat de l’auteur.
- Message transféré interdit : suppression sans sanction.
- Story partagée : suppression et bannissement immédiat.
- Mots interdits configurables : 1 jour de restriction, 3 jours en cas de récidive, puis bannissement.
- Règles configurables, publiées trois fois pendant chaque séance. Avec l’horaire 23 h–2 h, elles sont publiées vers 23 h, 0 h et 1 h.
- Horaires disponibles dans le panneau : **10 h–10 h 30 (test)**, 22 h–0 h, 23 h–1 h, 23 h–2 h et 0 h–3 h.
- Données persistantes dans PostgreSQL : options, horaires, règles, mots interdits, récidives, votes populaires et rappels déjà envoyés.
- Runtime léger : une seule connexion PostgreSQL, paramètres de modération conservés en mémoire et aucune utilisation du GPU.
- Point de contrôle Railway : `GET /health`.

## 1. Créer et préparer le bot Telegram

1. Ouvrez [@BotFather](https://t.me/BotFather), lancez `/newbot` et conservez le jeton secret.
2. Dans BotFather, désactivez le mode confidentialité du bot avec `/setprivacy`. Cela permet au bot de recevoir tous les messages à modérer.
3. Ajoutez le bot au groupe, puis nommez-le administrateur.
4. Accordez-lui au minimum les droits suivants :
   - supprimer les messages ;
   - bannir et restreindre les membres ;
   - inviter des utilisateurs et gérer les demandes d’adhésion.
5. Utilisez un **supergroupe** : les restrictions temporaires individuelles nécessitent ce type de groupe.

Les personnes déclarées dans `ADMIN_IDS` peuvent utiliser le panneau et sont exemptées de la modération. Pour qu’elles puissent aussi écrire pendant la fermeture, nommez-les administrateurs du groupe Telegram : les permissions par défaut d’un groupe fermé bloquent les membres ordinaires.

Les personnes déclarées dans `TRUSTED_IDS` ne sont exemptées **que des sanctions pour mots interdits**. Elles restent soumises à la fermeture, aux liens interdits, aux stories, aux transferts et à la justice populaire. Elles n’ont pas accès au panneau administrateur.

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
3. Ajoutez un service **PostgreSQL** au même projet Railway.
4. Ajoutez les variables suivantes dans l’onglet **Variables** du service du bot :

| Variable | Exemple | Obligatoire |
| --- | --- | --- |
| `BOT_TOKEN` | `123456789:secret` | Oui |
| `TARGET_CHAT_ID` | `-1001234567890` | Oui |
| `ADMIN_IDS` | `123456789,987654321` | Oui |
| `TRUSTED_IDS` | `111111111,222222222` | Non |
| `GROUP_INVITE_LINK` | `https://t.me/+...` | Recommandé |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Oui sur Railway |
| `TZ` | `Europe/Paris` | Non, valeur par défaut |
| `LOG_LEVEL` | `INFO` | Non |

La valeur doit être une **référence Railway**, exactement `DATABASE_URL=${{Postgres.DATABASE_URL}}`, sans recopier manuellement le mot de passe de la base. Le bot tolère également la valeur entourée de guillemets, comme `DATABASE_URL="${{Postgres.DATABASE_URL}}"`.

5. Aucun volume `/data` n’est nécessaire pour le bot lorsque PostgreSQL est configuré. Railway conserve les données dans le service PostgreSQL.
6. Gardez une seule réplique du service : un bot utilisant `getUpdates` ne doit pas avoir plusieurs processus de long polling actifs avec le même jeton.
7. Déployez. Le fichier `railway.json` sélectionne automatiquement le `Dockerfile`, installe Psycopg, démarre `python main.py` et utilise `/health` pour vérifier le service.

Le premier build télécharge l’image Python et installe les dépendances. Les déploiements suivants réutilisent la couche des dépendances tant que `requirements.txt` ne change pas. Un échec de démarrage n’attend plus cinq minutes : le healthcheck expire après 60 secondes et Railway limite les redémarrages automatiques à trois.

Railway documente cette connexion ici : [PostgreSQL sur Railway](https://docs.railway.com/databases/postgresql).

## 4. Utiliser le panneau

1. Ouvrez la conversation privée avec le bot.
2. Envoyez `/start`, puis `/panel`.
3. Utilisez les boutons :
   - `Automatique ON/OFF` ;
   - `Liens ON/OFF` ;
   - `Forwards ON/OFF` ;
   - `Justice ON/OFF` et `Seuil` ;
   - `Mots interdits` → Voir, Ajouter ou Supprimer ;
   - `Règles` → Voir/Modifier ou Publier maintenant ;
   - `Pub invitation` → modifier le texte, la photo et le lien du groupe de récompense, prévisualiser puis publier ;
   - `Pub invitation` → `Nouveaux liens pour tous` pour recréer les liens personnels et les renvoyer progressivement aux membres concernés ;
   - `Horaires` → choisir un créneau ;
   - `Resynchroniser` → réappliquer immédiatement les permissions et actualiser les administrateurs Telegram.

Quand l’ouverture automatique passe sur `OFF`, le groupe est fermé immédiatement et le bot publie :

> Aucune ouverture n’est prévue aujourd’hui. Revenez demain et partagez le groupe :  
> lien principal du groupe

Tous les messages de la séance sont supprimés immédiatement après le verrouillage, puis tout nouveau message d’un membre ordinaire est supprimé. Les identifiants sont conservés dans PostgreSQL : un redémarrage du bot pendant la séance ne fait pas perdre la liste à nettoyer. L’API Telegram ne permet toutefois pas au bot de relire librement l’ancien historique : les messages publiés avant l’installation de cette version ne peuvent pas être retrouvés rétroactivement.

## Publicité d’invitation et récompense

Le panneau conserve une seule publicité composée d’un texte et d’une photo. Toute modification remplace la version précédente. Le bouton `Publier` l’envoie dans le groupe avec le bouton **J’invite**.

Lorsqu’un membre utilise ce bouton :

1. Telegram ouvre la conversation privée avec le bot.
2. Le bot crée ou réutilise son lien personnel d’invitation, configuré pour demander l’approbation des administrateurs.
3. Lorsqu’une personne rejoint avec ce lien, est acceptée et satisfait à la validation interne, le compteur du parrain augmente une seule fois.
4. Le parrain reçoit une notification privée indiquant son nouveau compteur.
5. À **10 invitations validées**, le bot lui envoie une seule fois le lien du groupe de récompense configuré dans le panneau.

Le lien personnel enregistré contient aussi l’identifiant du groupe auquel il appartient. Si `TARGET_CHAT_ID` est remplacé, le bot met automatiquement tous les anciens profils en file de renouvellement au prochain démarrage. Un membre qui clique sur **J’invite** avant le traitement de cette file reçoit quand même immédiatement un lien du nouveau groupe. Les compteurs et les récompenses déjà acquises ne sont pas remis à zéro.

Le bouton **Nouveaux liens pour tous** force le même renouvellement à la demande. Le traitement se fait par petits lots afin de garder le bot réactif et de respecter les limites d’envoi de Telegram. Si Telegram échoue temporairement, le lien concerné reste en file et sera retenté. Si un utilisateur bloque les messages privés du bot, son nouveau lien reste enregistré et lui sera affiché à son prochain clic sur **J’invite**.

Lors d’un futur remplacement de groupe :

1. ajoutez le bot comme administrateur du nouveau supergroupe avec les droits requis ;
2. remplacez `TARGET_CHAT_ID` dans Railway et, si vous l’utilisez, `GROUP_INVITE_LINK` ;
3. redéployez le service : aucune suppression manuelle dans PostgreSQL n’est nécessaire ;
4. utilisez au besoin **Pub invitation → Nouveaux liens pour tous** pour relancer immédiatement l’envoi collectif.

Le délai et les contrôles de validation restent internes au bot et ne sont jamais indiqués dans ses messages publics ou privés. Un départ avant validation annule l’invitation en attente. Une même personne ne peut jamais être comptée deux fois, même après un redémarrage.

Cette fonction exige que le bot conserve le droit administrateur d’inviter des utilisateurs. Le bot ne valide ni ne refuse lui-même les demandes : les administrateurs du groupe gardent la décision. Les liens personnels, demandes en attente, compteurs et récompenses déjà envoyées sont conservés dans PostgreSQL.

## Commandes trusted

Les identifiants présents dans `TRUSTED_IDS` et `ADMIN_IDS` peuvent utiliser ces commandes dans le groupe. La commande doit être envoyée **en réponse** au message ou au média ciblé :

- `/supprime` : supprime le message ciblé. Si le message appartient à un album connu du bot, tout l’album est supprimé.
- `/pasfr` : empêche l’auteur d’écrire pendant 1 heure, puis 1 jour en cas de récidive, puis 5 jours, puis le bannit au quatrième signalement.
- `/ban` : bannit immédiatement l’auteur. Telegram reçoit l’option `revoke_messages=true`, qui supprime tous ses messages du supergroupe.

La commande est toujours supprimée, qu’elle soit autorisée ou non. Une personne absente de `TRUSTED_IDS` et `ADMIN_IDS` qui tente d’utiliser `/supprime`, `/pasfr` ou `/ban` reçoit 1 jour de restriction, puis 3 jours en cas de récidive, puis un bannissement à la troisième tentative. Cette progression est indépendante des sanctions pour mots interdits. Les administrateurs Telegram ne peuvent pas être restreints ou bannis par ce mécanisme. Les commandes `/pasfr` et `/ban` refusent aussi de sanctionner l’auteur de la commande ou un bot.

## Justice populaire

Quand cette option est sur `ON`, un membre peut répondre à un message, une photo, une vidéo ou un élément d’album avec uniquement `Pédo`, `Pedo`, `pédo`, `pedo` ou `Pdo`. La casse, les accents et une ponctuation finale sont ignorés.

- Un compte ne fournit qu’un vote par contenu ; ses doublons sont supprimés et ne font pas progresser le compteur.
- L’auteur ne peut pas voter contre son propre contenu et les comptes automatisés ne comptent pas.
- Tous les éléments d’un même album utilisent le même compteur.
- À 5 votes distincts par défaut, l’auteur est banni avec révocation de tous ses messages. Le contenu ciblé et tous les commentaires de signalement enregistrés sont supprimés.
- Le bot publie ensuite : « Merci à tous d’avoir lutté et d’avoir signalé. Le contenu a été supprimé et son auteur a été banni. »
- Les administrateurs Telegram sont protégés contre un bannissement collectif. Un `TRUSTED_ID` ordinaire ne l’est pas.
- Le bouton `Seuil` du panneau accepte une valeur de 2 à 50. Passer la justice populaire sur `OFF` supprime et remet à zéro les votes encore en attente.

Lorsque la justice populaire est sur `OFF`, écrire exactement l’un de ces mots-signalements ne crée aucun vote, ne supprime rien et ne déclenche pas non plus une sanction de mot interdit, même si `pedo` figure dans cette liste. Le message reste donc affiché pendant l’ouverture. Les règles générales restent prioritaires : un message envoyé pendant la fermeture est toujours supprimé.

Les votes, les albums suivis et les récidives `/pasfr` sont enregistrés dans PostgreSQL afin de rester cohérents après un redémarrage.

## Sécurité et comportement en cas de panne

- Le jeton n’est jamais écrit dans les logs. Conservez `BOT_TOKEN` uniquement dans les variables Railway.
- Au démarrage, le bot supprime un ancien webhook et abandonne les mises à jour devenues obsolètes, puis resynchronise immédiatement l’état du groupe.
- Si une personne ouvre manuellement le groupe hors horaires, le bot le referme au plus tard lors de la prochaine vérification (60 secondes). Les messages reçus entre-temps sont aussi supprimés.
- Si Telegram ne répond pas pendant la vérification du statut d’un membre, le contenu interdit est supprimé par sécurité, mais aucun bannissement ni restriction irréversible n’est appliqué sans confirmation.
- Si le processus redémarre pendant une séance, les rappels déjà enregistrés ne sont pas renvoyés en double.
- Si Telegram échoue temporairement pendant le nettoyage, seuls les identifiants réellement supprimés sont retirés de PostgreSQL et le reste est retenté toutes les 10 secondes.
- Le bot vérifie une dernière fois qu’un utilisateur n’est pas administrateur Telegram avant toute sanction irréversible.

## Dépannage : `Telegram getChat: 400 Bad Request: chat not found`

Cette erreur n’est pas causée par la RAM ou le processeur. Telegram indique que le bot associé à `BOT_TOKEN` ne peut pas accéder au chat demandé. Vérifiez ces trois points :

1. `TARGET_CHAT_ID` contient l’identifiant **numérique** du supergroupe, par exemple `-1001234567890`, et non son lien d’invitation `https://t.me/+...`.
2. Le bot créé avec ce même `BOT_TOKEN` est déjà présent dans ce groupe et nommé administrateur.
3. Le jeton n’appartient pas à un autre bot. Comparez le nom affiché par BotFather avec celui ajouté au groupe.

Après correction, redéployez. Si le problème persiste, arrêtez temporairement le service Railway, envoyez un message dans le groupe puis utilisez la commande `getUpdates` de la section 2 pour recopier exactement `message.chat.id`. Le nouveau message de démarrage du programme indique directement ces contrôles, sans imprimer plusieurs pages de traceback.

## Développement local

Copiez les variables de `.env.example` dans votre environnement, puis lancez :

```bash
python main.py
```

Exécutez les tests :

```bash
python -m unittest discover -v
```

La suite contient **80 tests** et vérifie notamment les **1 024 combinaisons** des règles de modération, les cinq créneaux horaires, dont le test de 30 minutes, le passage à minuit, les trusted IDs, les commandes non autorisées, les quatre niveaux `/pasfr`, les votes distincts, les albums, les administrateurs protégés, les fermetures automatique et manuelle, le nettoyage persistant et ses nouvelles tentatives, les échecs temporaires de Telegram ainsi que la publicité, les liens personnels associés au bon groupe, leur renouvellement collectif, la migration des anciennes données, la validation des adhésions, les compteurs et l’envoi unique de la récompense.

En local, si `DATABASE_URL` est absent, le bot utilise SQLite avec `DATABASE_PATH`. Sur Railway, PostgreSQL est automatiquement prioritaire dès que `DATABASE_URL` est présent.

## Référence Telegram

Le verrouillage utilise `setChatPermissions`, les sanctions utilisent `restrictChatMember` et `banChatMember`, le nettoyage groupé utilise `deleteMessages`, et les albums utilisent `Message.media_group_id`. Le parrainage utilise `createChatInviteLink` avec demandes d’adhésion, les mises à jour `chat_join_request` et `chat_member`, ainsi que le [deep linking Telegram](https://core.telegram.org/bots/features#deep-linking). Selon l’[API officielle Telegram Bot](https://core.telegram.org/bots/api), `revoke_messages=true` supprime tous les messages de l’utilisateur banni et cette révocation est toujours active dans les supergroupes.
