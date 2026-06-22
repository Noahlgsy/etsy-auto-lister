# Déploiement cloud (Fly.io) — dashboard Ventes / Trésorerie / Comptabilité

Ce guide met **en ligne** la partie tableau de bord pour que Noah **et** Théo y
accèdent depuis n'importe où, avec **données synchronisées** (une seule base).

> La génération d'images (Atelier / Easy Picture / Flow) **reste en local** —
> elle a besoin de Chrome. En ligne, seuls **Ventes**, **Trésorerie** et
> **Comptabilité** sont affichés, derrière un **mot de passe**.

Tout se fait depuis le dossier du projet, dans le Terminal.

---

## 0. Pré-requis (à faire une fois, en local)
L'autorisation Etsy doit déjà être faite en local (elle l'est : ton `.env`
contient le `ETSY_REFRESH_TOKEN`). On va recopier ces valeurs comme « secrets »
côté serveur — **jamais dans le code/git**.

Affiche tes valeurs pour les avoir sous la main (ne les partage pas) :
```bash
cat .env | grep -E '^ETSY_(KEYSTRING|SHARED_SECRET|REFRESH_TOKEN|SHOP_ID|USER_ID)='
```

## 1. Installer l'outil Fly + se connecter
```bash
brew install flyctl          # (ou : curl -L https://fly.io/install.sh | sh)
fly auth signup              # crée le compte (ou `fly auth login` si déjà fait)
```
> Fly demande une carte (anti-abus). Une appli aussi légère reste normalement
> dans le gratuit ; tu peux fixer une limite dans le dashboard Fly.

## 2. Créer l'application
Depuis le dossier du projet (le `fly.toml` et le `Dockerfile` sont déjà prêts) :
```bash
fly launch --no-deploy --copy-config
```
- Si le nom `etsy-dashboard` est pris, Fly en propose un autre (accepte).
- Région : choisis **Paris (cdg)**. Réponds **non** à « Postgres » / « Redis ».

## 3. Créer le disque persistant (base + token)
```bash
fly volumes create etsy_data --region cdg --size 1   # 1 Go, large
```

## 4. Déposer les secrets
Génère une clé de session, choisis un mot de passe d'accès, et colle tes valeurs
Etsy (de l'étape 0) :
```bash
fly secrets set \
  APP_PASSWORD='choisis-un-mot-de-passe-solide' \
  SESSION_SECRET="$(openssl rand -hex 32)" \
  ETSY_KEYSTRING='...' \
  ETSY_SHARED_SECRET='...' \
  ETSY_REFRESH_TOKEN='...' \
  ETSY_SHOP_ID='...' \
  ETSY_USER_ID='...'
```
> Pas besoin de la clé Anthropic en ligne (elle ne sert qu'à la génération de
> texte, qui reste locale).

## 5. Déployer
```bash
fly deploy
```
À la fin, Fly affiche l'URL (ex. `https://etsy-dashboard.fly.dev`).

## 6. Récupérer tes données déjà saisies (important)
Tes coûts d'achat, « payé par Noah/Théo », commentaires… vivent dans
`data/finance.db`. Pour les retrouver en ligne, envoie ce fichier sur le volume :
```bash
fly sftp shell
# au prompt qui s'ouvre :
put data/finance.db /data/finance.db
exit
fly apps restart etsy-dashboard      # (mets ton vrai nom d'appli)
```
> Si tu sautes cette étape, la version en ligne repart à vide : les commandes se
> re-synchronisent depuis Etsy, mais les coûts/payeurs saisis ne seront pas là.

## 7. Utiliser
Ouvre l'URL → page de connexion → entre `APP_PASSWORD`. Partage l'URL + le mot
de passe avec ton frère. Cliquez **Synchroniser** pour rafraîchir les ventes.

---

## Au quotidien
- **Mettre à jour le code** : `fly deploy` (le volume `data` est conservé).
- **Voir les logs** : `fly logs`.
- **Changer un secret** : `fly secrets set CLE='nouvelle'` puis `fly deploy`.
  ⚠️ Le `ETSY_REFRESH_TOKEN` tourne tout seul côté serveur (stocké sur le
  volume) — n'y touche pas, sinon tu casses la connexion Etsy.
- **Coût / mise en veille** : l'appli s'éteint quand personne ne l'utilise et se
  rallume en quelques secondes à la 1ʳᵉ visite (réglé dans `fly.toml`).

## Sécurité
- Aucune donnée perso n'est dans le code/git (`.env`, `finance.db` ignorés).
- Tout est derrière le mot de passe `APP_PASSWORD` ; HTTPS forcé par Fly.
- Pour révoquer l'accès : change `APP_PASSWORD` (`fly secrets set …`).
