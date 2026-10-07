# Setting Up SSH Access to GitHub

Follow these steps to connect your computer to GitHub using SSH. This lets you
push and pull code without typing a password each time.

> Run all commands in a terminal:
> - **macOS:** open the **Terminal** app
> - **Windows:** open **Git Bash** (install from https://git-scm.com if needed)
> - **Linux:** open your terminal

---

## 1. Check if you already have a key

```bash
ls -la ~/.ssh
```

If you see a file named `id_ed25519.pub`, you already have a key — **skip to Step 3**.
Otherwise, continue.

---

## 2. Generate a new SSH key

Replace the email with your own GitHub email:

```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
```

- When asked **"Enter file in which to save the key"** → press **Enter** (use default).
- When asked for a **passphrase** → press **Enter** twice for none, or type one for extra security.

---

## 3. Start the SSH agent and add your key

**macOS:**
```bash
eval "$(ssh-agent -s)"
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

**Windows (Git Bash) / Linux:**
```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
```

---

## 4. Copy your PUBLIC key

**macOS:**
```bash
pbcopy < ~/.ssh/id_ed25519.pub
```

**Windows (Git Bash):**
```bash
cat ~/.ssh/id_ed25519.pub | clip
```

**Linux:**
```bash
cat ~/.ssh/id_ed25519.pub
```
(then select and copy the output)

> ⚠️ Only ever share the file ending in **`.pub`**.
> Never share the private key (`id_ed25519` with no extension) — that's your secret.

---

## 5. Add the key to GitHub

1. Go to **https://github.com/settings/ssh/new** (log in if needed).
2. **Title:** give it a name, e.g. "My Laptop".
3. **Key type:** leave as "Authentication Key".
4. **Key:** paste (Cmd+V / Ctrl+V) the key you copied in Step 4.
5. Click **Add SSH key**.

---

## 6. Test the connection

```bash
ssh -T git@github.com
```

Type `yes` if asked to confirm the fingerprint. You should see:

```
Hi <your-username>! You've successfully authenticated...
```

✅ You're connected. You can now clone, push, and pull over SSH.

---

## 7. Clone a repository (example)

```bash
git clone git@github.com:OWNER/REPO.git
```

If you already have a local folder and want to connect it:

```bash
cd your-folder
git init
git remote add origin git@github.com:OWNER/REPO.git
git branch -M main
git add .
git commit -m "first commit"
git push -u origin main
```

---

### Troubleshooting
- **"Permission denied (publickey)"** → the key wasn't added to GitHub, or the agent
  isn't running. Redo Steps 3–5.
- **"Repository not found"** → you don't have access to that repo, or the URL is wrong.
  Ask the repo owner to add you as a collaborator.
