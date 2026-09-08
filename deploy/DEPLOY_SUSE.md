# Deploying ZSTM on SUSE Linux Enterprise Server 15 SP7

Target: `SLES 15 SP7` (x86-64, VMware). Serves the app with **gunicorn over
HTTPS**, managed by **systemd**, posting real 311 moves via **pyrfc**, with a
TLS cert signed by the existing **Kassim Local CA**.

Throughout, replace `<VM_IP>` with the VM's LAN address (find it with
`ip -4 addr show | grep inet`). Commands are run as `root` unless noted.

> **Which path?** Sections 1–11 assume the VM is **registered** with SUSE (so
> `zypper` can install `python311`, `gcc`, `git`, …). If the box is **not
> registered** — `zypper` says *"No provider of …"* and `SUSEConnect
> --status-text` shows *Not Registered* — jump to **[Appendix A](#appendix-a--no-registration-install-self-contained-python)**,
> which is the exact path used for the first production box (`10.10.2.50`): a
> self-contained Python 3.11 (no repos) plus SAP's prebuilt pyrfc wheel. Do the
> TLS cert (§8) and read the gotchas at the end either way.

---

## 0. One-time facts to gather

- `<VM_IP>` — the VM's LAN IP (e.g. `10.3.2.50`).
- The SAP application server host/sysnr/client + a service user (for `.env`).
- The HANA host/port/user (for `.env`, used by the doff lookup).
- The **SAP NW RFC SDK for Linux** zip (from SAP Support Portal — you need an
  S-user / valid license). File looks like `nwrfc750P_x-70002752.zip`.

Optionally give the box a name:

```bash
hostnamectl set-hostname zstm
```

---

## 1. OS packages

```bash
zypper refresh
zypper install -y python311 python311-pip python311-devel git unzip gcc gcc-c++
```

(The build tools are only needed if you have to *build* pyrfc; skip if you use a
prebuilt wheel — see step 6.)

---

## 2. Service user + app directory

```bash
useradd --system --create-home --home-dir /opt/zstm --shell /usr/sbin/nologin zstm
install -d -o zstm -g zstm /opt/zstm
```

---

## 3. Get the code onto the VM

Either clone (if the VM can reach GitHub) or copy from your dev box.

```bash
# option A: clone
sudo -u zstm git clone https://github.com/rehannhussain/ZSTM.git /opt/zstm

# option B: from your machine, scp the working tree (excludes are fine):
#   scp -r D:/Projects/SAP/ZSTM/*  user@<VM_IP>:/tmp/zstm  && mv ...
```

> The repo does **not** contain `webapp/resources/` (the OpenUI5 runtime, ~540 MB,
> gitignored) or `server/.env` / certs — those are handled below.

---

## 4. OpenUI5 runtime (required — the UI is blank without it)

If the VM has internet:

```bash
sudo -u zstm bash -c '
  cd /opt/zstm
  curl -sL -o /tmp/ui5.zip https://github.com/SAP/openui5/releases/download/1.120.30/openui5-runtime-1.120.30.zip
  python3.11 -c "import zipfile; z=zipfile.ZipFile(\"/tmp/ui5.zip\"); z.extractall(\"webapp\",[n for n in z.namelist() if n.startswith(\"resources/\") and not n.endswith(\"/\")])"
'
```

No internet on the VM? Download that zip elsewhere and `scp` it, then run the
same `python3.11 -c ...` extract step. Verify: `ls /opt/zstm/webapp/resources/sap-ui-core.js`.

---

## 5. Python venv + app deps

```bash
sudo -u zstm python3.11 -m venv /opt/zstm/.venv
sudo -u zstm /opt/zstm/.venv/bin/pip install --upgrade pip
sudo -u zstm /opt/zstm/.venv/bin/pip install -r /opt/zstm/deploy/requirements-prod.txt
```

This installs `flask`, `hdbcli`, and `gunicorn`. `pyrfc` is next.

---

## 6. SAP NW RFC SDK + pyrfc (for real 311 posting)

**6a. Install the SDK** (as root):

```bash
mkdir -p /usr/local/sap
unzip /path/to/nwrfc750P_x-70002752.zip -d /usr/local/sap      # creates /usr/local/sap/nwrfcsdk
# make the shared libs discoverable system-wide
echo /usr/local/sap/nwrfcsdk/lib > /etc/ld.so.conf.d/nwrfcsdk.conf
ldconfig
ldconfig -p | grep sapnwrfc      # should list libsapnwrfc.so
```

**6b. Install pyrfc** into the venv. Preferred: a prebuilt wheel from
<https://github.com/SAP/PyRFC/releases> that matches **cp311** + Linux x86_64
(e.g. `pyrfc-3.3.1-cp311-cp311-manylinux_2_17_x86_64.whl`):

```bash
# scp the wheel to the VM first, then:
sudo -u zstm /opt/zstm/.venv/bin/pip install /path/to/pyrfc-3.3.1-cp311-*.whl
```

If no matching wheel exists, build from source (needs step 1's compilers):

```bash
sudo -u zstm bash -c '
  export SAPNWRFC_HOME=/usr/local/sap/nwrfcsdk
  export LD_LIBRARY_PATH=$SAPNWRFC_HOME/lib
  /opt/zstm/.venv/bin/pip install cython wheel
  git clone https://github.com/SAP/PyRFC /tmp/PyRFC && cd /tmp/PyRFC
  git checkout 3.3.1
  /opt/zstm/.venv/bin/python setup.py bdist_wheel
  /opt/zstm/.venv/bin/pip install dist/pyrfc-*.whl
'
```

Verify (must print a version, no ImportError):

```bash
sudo -u zstm bash -c 'SAPNWRFC_HOME=/usr/local/sap/nwrfcsdk LD_LIBRARY_PATH=/usr/local/sap/nwrfcsdk/lib /opt/zstm/.venv/bin/python -c "import pyrfc; print(pyrfc.__version__)"'
```

> Skipping RFC for now? You can defer steps 6a/6b — the app runs and scans, and
> posting returns a clear "pyrfc not installed" error until you add it.

---

## 7. Configuration (`.env`)

```bash
sudo -u zstm cp /opt/zstm/server/.env.example /opt/zstm/server/.env
sudo -u zstm nano /opt/zstm/server/.env
chmod 600 /opt/zstm/server/.env
```

Set the real values:

- `SAP_ASHOST`, `SAP_SYSNR`, `SAP_CLIENT`, `SAP_USER`, `SAP_PASSWD` (or `SAP_DEST`)
- `HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`
- `SAP_RFC_MOCK=false`
- `PORT` / `USE_HTTPS` are **ignored** under gunicorn (systemd sets the bind +
  TLS), so they don't matter here.

Confirm the VM can actually reach SAP + HANA: `nc -vz <SAP_ASHOST> 33<sysnr>`
and `nc -vz <HANA_HOST> <HANA_PORT>`.

---

## 8. TLS certificate (reuse the Kassim CA)

On the **machine that holds the CA key** (your Windows dev box, in Git Bash):

```bash
bash deploy/make-vm-cert.sh <VM_IP> zstm
```

That writes `server/vm-cert.pem` and `server/vm-key.pem`. Copy them to the VM:

```bash
scp server/vm-cert.pem  user@<VM_IP>:/tmp/cert.pem
scp server/vm-key.pem   user@<VM_IP>:/tmp/key.pem
```

On the VM:

```bash
install -o zstm -g zstm -m 644 /tmp/cert.pem /opt/zstm/server/cert.pem
install -o zstm -g zstm -m 600 /tmp/key.pem  /opt/zstm/server/key.pem
rm -f /tmp/cert.pem /tmp/key.pem
```

The devices already trust the CA, so they'll trust this cert with no warning.
(If a device is new, install `rootCA.pem` on it — see the PWA notes.)

---

## 9. systemd service

```bash
cp /opt/zstm/deploy/zstm.service /etc/systemd/system/zstm.service
# review paths/user if you deviated from /opt/zstm or the zstm user
systemctl daemon-reload
systemctl enable --now zstm
systemctl status zstm --no-pager
journalctl -u zstm -n 40 --no-pager      # should show gunicorn "Listening at: https://0.0.0.0:8010"
```

---

## 10. Firewall

```bash
firewall-cmd --permanent --add-port=8010/tcp
firewall-cmd --reload
```

---

## 11. Verify

```bash
# on the VM — trusted-chain check
openssl s_client -connect 127.0.0.1:8010 -CAfile /opt/zstm/server/cert.pem </dev/null 2>/dev/null | grep -E "issuer=|Verify return code"
curl --cacert /opt/zstm/server/cert.pem https://<VM_IP>:8010/api/health
```

Then from an iPad/phone on the LAN: open `https://<VM_IP>:8010` in the browser
(no cert warning if the CA is installed), scan a doff, and Add → Post move.
Install to the home screen for the standalone app.

---

## Updating later

If `git` is installed (registered box):

```bash
sudo -u zstm git -C /opt/zstm pull
sudo -u zstm /opt/zstm/.venv/bin/pip install -r /opt/zstm/deploy/requirements-prod.txt
systemctl restart zstm
```

No `git` (Appendix A box) — overlay the source from the GitHub archive, which
preserves `.env`, certs, and `webapp/resources/`:

```bash
curl -sL -o /tmp/zstm.zip https://github.com/rehannhussain/ZSTM/archive/refs/heads/main.zip
unzip -q -o /tmp/zstm.zip -d /tmp
cp -a /tmp/ZSTM-main/webapp/. /opt/zstm/webapp/ && cp -a /tmp/ZSTM-main/server/. /opt/zstm/server/
chown -R zstm:zstm /opt/zstm && systemctl restart zstm
```

## Troubleshooting

- **`zypper` says "No provider of …" / `SUSEConnect --status-text` = Not
  Registered** → the box has no repos. Register + enable modules (§1 note), or
  use the no-repo path in [Appendix A](#appendix-a--no-registration-install-self-contained-python).
- **Package names on SLES** → the git binary is **`git-core`** (not `git`);
  `python311`/`gcc`/`gcc-c++` live in the python3 / development-tools modules.
- **`chown: invalid group 'zstm'`** → `useradd` here doesn't create a matching
  group. Run `groupadd -f zstm && usermod -g zstm zstm` before the `chown` (the
  systemd unit's `Group=zstm` needs it too).
- **No `nano`** → use `vi`: `i` to edit, `Esc` then `:wq!` to save. `:wq!` (with
  the `!`) is needed because a mode-600 file opens read-only for root.
- **Edit `.env` with `vi`, not `sed`** when a password has `$ \ " '` etc. — the
  shell mangles those. `sed` is fine only for simple values like `SAP_RFC_MOCK`.
- **OpenUI5 "File is not a zip file"** → the `ui5.zip` download was truncated.
  Re-fetch with `curl -fL` (the `-f` fails loudly instead of saving an error
  page) and confirm with `python3 -c "import zipfile; zipfile.ZipFile('/tmp/ui5.zip')"`.
- **`/api/health` shows `"mock":true`** → `SAP_RFC_MOCK` is still `true` in
  `.env`. `sed -i 's/^SAP_RFC_MOCK=.*/SAP_RFC_MOCK=false/' .env` and restart.
- **Scan fails `(10, 'authentication failed')` but a direct `hdbcli` connect
  works** → a gunicorn worker was started before `.env` was saved. `systemctl
  restart zstm`. **Always restart after any `.env` change** — the app reads
  `.env` once at import.
- **502 / won't start, `ImportError ... sapnwrfc`** → SDK not on the linker path.
  Check `ldconfig -p | grep sapnwrfc` and that the unit has `LD_LIBRARY_PATH`.
- **Posting fails, HANA/SAP unreachable** → firewall between the VM and SAP. Test
  without extra tools: `(echo > /dev/tcp/<HOST>/<PORT>) 2>/dev/null && echo open`.
- **Blank UI** → `webapp/resources/` missing (step 4 / A‑2).
- **Cert warning on device** → that device doesn't have `rootCA.pem` installed,
  or the cert's SAN doesn't include the IP/host you typed in the URL.
- **AppArmor** (SLES default, not SELinux) rarely blocks this; if gunicorn can't
  read the cert, check file ownership/mode from step 8.

---

## Appendix A — no-registration install (self-contained Python)

Use this when the VM **isn't registered** with SUSE, so `zypper` can't install
anything. It needs no repos and no compiler: a portable Python 3.11 fetched by
[`uv`](https://astral.sh/uv), plus SAP's **prebuilt** pyrfc wheel. This is the
exact path used for the first box (`10.10.2.50`). Only `unzip` and `curl` from
the base image are required. Run as `root` unless noted.

Prereqs already on the VM: `/tmp/nwrfc750P_19-70002752.zip` (the SDK, from your
S-user), and `/tmp/cert.pem` + `/tmp/key.pem` (minted with
`deploy/make-vm-cert.sh <VM_IP>` on the box that holds the CA — §8).

**A‑1. Service user + group** (`useradd` doesn't make the group):

```bash
useradd --system --create-home --home-dir /opt/zstm --shell /usr/sbin/nologin zstm
groupadd -f zstm && usermod -g zstm zstm
```

**A‑2. Code (via GitHub zip — no `git`), SDK, OpenUI5, certs:**

```bash
# app code — the archive excludes the gitignored webapp/resources/
curl -sL -o /tmp/zstm.zip https://github.com/rehannhussain/ZSTM/archive/refs/heads/main.zip
unzip -q -o /tmp/zstm.zip -d /tmp
rm -rf /opt/zstm && mv /tmp/ZSTM-main /opt/zstm

# SAP NW RFC SDK
mkdir -p /usr/local/sap
unzip -o /tmp/nwrfc750P_19-70002752.zip -d /usr/local/sap
echo /usr/local/sap/nwrfcsdk/lib > /etc/ld.so.conf.d/nwrfcsdk.conf
ldconfig && ldconfig -p | grep sapnwrfc

# OpenUI5 runtime into webapp/resources (system python3 is fine just to unzip)
curl -fL -o /tmp/ui5.zip https://github.com/SAP/openui5/releases/download/1.120.30/openui5-runtime-1.120.30.zip
python3 -c "import zipfile; z=zipfile.ZipFile('/tmp/ui5.zip'); z.extractall('/opt/zstm/webapp',[n for n in z.namelist() if n.startswith('resources/') and not n.endswith('/')])"
ls /opt/zstm/webapp/resources/sap-ui-core.js

# certs + config skeleton, then ownership
cp /tmp/cert.pem /opt/zstm/server/cert.pem
cp /tmp/key.pem  /opt/zstm/server/key.pem
cp /opt/zstm/server/.env.example /opt/zstm/server/.env
chown -R zstm:zstm /opt/zstm
chmod 644 /opt/zstm/server/cert.pem && chmod 600 /opt/zstm/server/key.pem /opt/zstm/server/.env
```

**A‑3. Self-contained Python 3.11 + deps + prebuilt pyrfc** (as the `zstm` user
so nothing lands in `/root`):

```bash
# official prebuilt pyrfc wheel — keep the real filename (pip needs the tags)
curl -fL -o /tmp/pyrfc-3.3.1-cp311-cp311-linux_x86_64.whl \
  https://github.com/SAP-archive/PyRFC/releases/download/v3.3.1/pyrfc-3.3.1-cp311-cp311-linux_x86_64.whl

su -s /bin/bash zstm -c '
  set -e
  export HOME=/opt/zstm
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH=$HOME/.local/bin:$PATH
  uv python install 3.11
  uv venv --python 3.11 /opt/zstm/.venv
  uv pip install --python /opt/zstm/.venv/bin/python -r /opt/zstm/deploy/requirements-prod.txt
  uv pip install --python /opt/zstm/.venv/bin/python /tmp/pyrfc-3.3.1-cp311-cp311-linux_x86_64.whl
'

# verify the whole stack loads (must print a version)
SAPNWRFC_HOME=/usr/local/sap/nwrfcsdk LD_LIBRARY_PATH=/usr/local/sap/nwrfcsdk/lib \
  /opt/zstm/.venv/bin/python -c "import pyrfc, flask, hdbcli; print('pyrfc', pyrfc.__version__)"
```

**A‑4. Fill `.env`, then start the service.** Edit with `vi` (see gotchas), set
the real `SAP_*` / `HANA_*` values and **`SAP_RFC_MOCK=false`**:

```bash
vi /opt/zstm/server/.env
chown zstm:zstm /opt/zstm/server/.env && chmod 600 /opt/zstm/server/.env
# sanity-check HANA creds directly (prints the real error if wrong):
/opt/zstm/.venv/bin/python - <<'PY'
import os
for l in open('/opt/zstm/server/.env'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); os.environ[k.strip()]=v.strip().strip('"').strip("'")
from hdbcli import dbapi
try:
    c=dbapi.connect(address=os.environ['HANA_HOST'], port=int(os.environ['HANA_PORT']),
        user=os.environ['HANA_USER'], password=os.environ['HANA_PASSWORD'],
        encrypt=os.environ.get('HANA_ENCRYPT','true').lower()=='true', sslValidateCertificate=False)
    c.cursor().execute('SELECT 1 FROM DUMMY'); print('HANA OK'); c.close()
except Exception as e: print('HANA FAILED:', e)
PY

cp /opt/zstm/deploy/zstm.service /etc/systemd/system/zstm.service
systemctl daemon-reload
systemctl enable --now zstm
firewall-cmd --permanent --add-port=8010/tcp 2>/dev/null && firewall-cmd --reload || echo "no firewalld"
```

**A‑5. Verify:**

```bash
curl --cacert /opt/zstm/server/cert.pem https://127.0.0.1:8010/api/health          # want "mock":false
curl --cacert /opt/zstm/server/cert.pem -X POST https://127.0.0.1:8010/api/stock/resolve \
  -H 'Content-Type: application/json' -d '{"qr":"<a-real-doff-qr>"}'                # want a lines[] payload
```

Then open `https://<VM_IP>:8010` on a device with `rootCA.pem` installed.

> Register the OS later when a code is available — the app keeps running, but a
> registered SLES is what gets security patches and SUSE support. After
> registering you can `zypper install git-core` and switch to `git pull` updates.
