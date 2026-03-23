# WhatsApp Bridge — Module ClawbotCore

Bridge WhatsApp pour ClawbotCore basé sur Baileys (WhatsApp Web multi-device).

- **Port** : 3100
- **Service** : `whatsapp-bridge`
- **Auth** : session persistée dans `auth/`

## Endpoints HTTP directs

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/status` | Statut connexion + QR code |
| POST | `/send` | Envoyer un message texte |
| POST | `/send-image` | Envoyer une image |
| POST | `/send-video` | Envoyer une vidéo |
| POST | `/send-audio` | Envoyer un vocal (PTT) |
| POST | `/send-file` | Envoyer un document |
| POST | `/send-location` | Envoyer une localisation |
| POST | `/send-contact` | Envoyer une vCard |
| POST | `/v1/whatsapp-bridge/execute` | Endpoint unifié orchestrateur |
| POST | `/v1/whatsapp-bridge/:toolName/execute` | Endpoint par outil |

## Outils agents (24)

Tous les outils sont préfixés `whatsapp-bridge__` par l'orchestrateur.
Le JID format est `XXXXXXXXX@s.whatsapp.net` (contact) ou `XXXXXXXXX@g.us` (groupe).

---

### Cat. 1 — Messaging (7 outils)

#### `send_message`
Envoyer un message texte via WhatsApp.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 (ex: +33612345678) ou group JID |
| `text` | string | oui | Texte du message |
| `quote_message_id` | string | non | ID du message à citer (reply) |

#### `send_image`
Envoyer une image depuis un fichier local.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `image_path` | string | oui | Chemin absolu vers l'image (jpg, png, gif, webp) |
| `caption` | string | non | Légende optionnelle |

#### `send_video`
Envoyer une vidéo depuis un fichier local.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `video_path` | string | oui | Chemin absolu vers la vidéo (mp4, 3gp) |
| `caption` | string | non | Légende optionnelle |

#### `send_audio`
Envoyer un message vocal (PTT). Le fichier doit être au format OGG/Opus.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `audio_path` | string | oui | Chemin absolu vers le .ogg opus |

#### `send_file`
Envoyer un document (PDF, ZIP, TXT, etc.).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `file_path` | string | oui | Chemin absolu vers le fichier |
| `filename` | string | non | Nom d'affichage (auto-détecté si omis) |

#### `send_location`
Envoyer un pin de localisation GPS.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `latitude` | number | oui | Latitude (ex: 48.8566) |
| `longitude` | number | oui | Longitude (ex: 2.3522) |
| `name` | string | non | Nom du lieu (ex: "Tour Eiffel") |
| `address` | string | non | Adresse texte |

#### `send_contact`
Envoyer une carte de visite (vCard).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `to` | string | oui | Numéro E.164 ou group JID |
| `contact_name` | string | oui | Nom complet du contact |
| `contact_phone` | string | oui | Numéro du contact |

---

### Cat. 2 — Message actions (5 outils)

#### `react_to_message`
Réagir à un message avec un emoji. Emoji vide = retirer la réaction.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `message_id` | string | oui | ID du message cible |
| `emoji` | string | oui | Emoji (ex: "👍", "❤️"). Vide pour retirer. |

#### `edit_message`
Modifier un message envoyé (ses propres messages uniquement, dans les 15 min).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `message_id` | string | oui | ID du message à modifier |
| `new_text` | string | oui | Nouveau contenu texte |

#### `delete_message`
Supprimer un message pour tous (ses propres messages, ~2 jours max).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `message_id` | string | oui | ID du message à supprimer |

#### `pin_message`
Épingler ou désépingler un message dans un chat.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `message_id` | string | oui | ID du message |
| `pin` | boolean | oui | `true` = épingler, `false` = désépingler |

#### `reply_to_message`
Répondre à un message spécifique (citation). Utiliser `send_message` avec `quote_message_id`.

> Note : pas un outil distinct — utiliser `send_message` + paramètre `quote_message_id`.

---

### Cat. 3 — Chat management (5 outils)

#### `pin_chat`
Épingler/désépingler une conversation en haut de la liste.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `pin` | boolean | oui | `true` = épingler, `false` = désépingler |

#### `archive_chat`
Archiver/désarchiver une conversation.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `archive` | boolean | oui | `true` = archiver, `false` = désarchiver |

#### `mute_chat`
Muter/démuter une conversation.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `mute` | boolean | oui | `true` = muter, `false` = démuter |
| `duration` | string | non | Durée : `"8h"`, `"1w"`, ou `"forever"` (défaut) |

#### `mark_read`
Marquer une conversation comme lue ou non-lue.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |
| `read` | boolean | oui | `true` = lu, `false` = non-lu |

#### `delete_chat`
Supprimer une conversation entière (irréversible).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | JID du chat |

---

### Cat. 4 — Groups (4 outils)

#### `create_group`
Créer un groupe WhatsApp.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `name` | string | oui | Nom du groupe |
| `participants` | array[string] | oui | Numéros E.164 des membres à ajouter |

#### `group_info`
Obtenir les infos complètes d'un groupe : nom, description, membres, admins, paramètres.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | Group JID (ex: `120363xxxx@g.us`) |

#### `group_update`
Modifier un groupe : nom, description, mode admin-only.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | Group JID |
| `subject` | string | non | Nouveau nom |
| `description` | string | non | Nouvelle description |
| `setting` | string | non | `"announcement"` (admin-only), `"not_announcement"`, `"locked"`, `"unlocked"` |

#### `group_manage_members`
Ajouter, retirer, promouvoir ou rétrograder des membres.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | Group JID |
| `participants` | array[string] | oui | Numéros E.164 des membres |
| `action` | string | oui | `"add"`, `"remove"`, `"promote"`, `"demote"` |

---

### Cat. 5 — Contacts & Info (3 outils)

#### `get_status`
Vérifier le statut de connexion du bridge WhatsApp. Aucun paramètre.

**Réponse** : `{ connected, status, phone }`

#### `check_whatsapp`
Vérifier si des numéros de téléphone ont un compte WhatsApp.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `phones` | array[string] | oui | Numéros E.164 à vérifier |

**Réponse** : `{ results: [{ jid, exists }] }`

#### `get_profile_info`
Obtenir la photo de profil, le statut texte et les infos business d'un contact.

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `jid` | string | oui | Contact JID |

**Réponse** : `{ picture, status, business }`

---

### Cat. 6 — Utility (1 outil)

#### `list_recent_media`
Lister les médias WhatsApp reçus récemment (fichiers `/tmp/wa_*`).

| Param | Type | Requis | Description |
|-------|------|--------|-------------|
| `limit` | number | non | Max fichiers (défaut: 20) |
| `type_filter` | string | non | Filtre : `"image"`, `"audio"`, `"video"`, `"file"`, ou `"all"` (défaut) |

**Réponse** : `{ files: [{ path, type, size, modified }] }`

---

## Réception inbound (messages entrants)

Le bridge transmet automatiquement les messages reçus au core via `POST /v1/channels/whatsapp/inbound`.

| Type reçu | Traitement | Fichier créé |
|-----------|------------|--------------|
| Texte | Texte brut | — |
| Image | Download buffer → fichier | `/tmp/wa_img_{id}.jpg` |
| Audio/Vocal | Download buffer → fichier | `/tmp/wa_audio_{id}.ogg` |
| Vidéo | Download buffer → fichier | `/tmp/wa_video_{id}.mp4` |
| Document | Download buffer → fichier | `/tmp/wa_file_{id}{ext}` |
| Sticker | Download buffer → fichier | `/tmp/wa_sticker_{id}.webp` |
| Location | Texte `Location: lat,lng (name)` | — |
| Contact/vCard | Texte avec nom(s) du contact | — |

## Limites connues

- **Images/Audio/Vidéo** : ~16 MB max
- **Documents** : ~100 MB max
- **Edit message** : 15 min après envoi, messages propres uniquement
- **Delete for all** : ~2 jours après envoi
- **Groupes** : max 1024 membres
- **chatModify** (pin/archive/mute) : le chat doit exister dans l'historique local Baileys
- **Pas de GPS** : le Pi n'a pas de GPS, la localisation par IP n'est qu'une approximation ville
