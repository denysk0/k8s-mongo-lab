# k8s-mongo-lab

![kubernetes](https://img.shields.io/badge/kubernetes-1.28+-326ce5?logo=kubernetes&logoColor=white)
![mongodb](https://img.shields.io/badge/mongodb-7-47A248?logo=mongodb&logoColor=white)
![python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-green)

MongoDB high-availability lab on a local Kubernetes cluster (kind). Three-node replica set, automatic failover, ACID demo.

Full walkthrough: **[`mongo-lab.md`](mongo-lab.md)**.

```bash
make up
```

# Prerequisites

Tested on macOS (Apple Silicon) and Linux. Windows users: WSL2 recommended.

## Required tools

| Tool | Version | Purpose |
|---|---|---|
| Docker | 20+ | runs kind nodes as containers |
| kind | 0.20+ | local Kubernetes cluster |
| kubectl | 1.28+ | Kubernetes CLI |
| Python | 3.10+ | demo app + seed script |
| make | any | lab lifecycle targets |

---

## macOS (Homebrew)

```bash
brew install --cask docker
brew install kind kubectl python@3.12
```

Start Docker Desktop once before continuing.

---

## Linux (Debian/Ubuntu)

Docker:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # re-login after this
```

kind (binary install):

```bash
# pick arch: amd64 or arm64
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind
```

kubectl:

```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
```

Python:

```bash
sudo apt install -y python3 python3-venv python3-pip make
```

---

## Verify

```bash
docker version
kind version
kubectl version --client
python3 --version
```

---

## Python virtualenv for the demo app

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`:

```
flask>=3.0
pymongo>=4.6
```
