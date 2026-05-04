import sys
from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:30017,localhost:30018,localhost:30019/?replicaSet=rs0&serverSelectionTimeoutMS=3000"
DIRECT_URIS = [
    "mongodb://localhost:30017/?directConnection=true",
    "mongodb://localhost:30018/?directConnection=true",
    "mongodb://localhost:30019/?directConnection=true",
]


def connect():
    try:
        c = MongoClient(MONGO_URI)
        c.admin.command("hello")
        _ = c.primary
        print(f"Connected via replicaSet URI. PRIMARY={c.primary}")
        return c
    except Exception as e:
        print(f"replicaSet URI failed ({e.__class__.__name__}); probing direct URIs...")
    for uri in DIRECT_URIS:
        try:
            c = MongoClient(uri, serverSelectionTimeoutMS=2000)
            hello = c.admin.command("hello")
            if hello.get("isWritablePrimary"):
                print(f"Connected directly to PRIMARY at {uri}")
                return c
            c.close()
        except Exception as e:
            print(f"  {uri}: {e.__class__.__name__}")
    raise RuntimeError("No PRIMARY reachable on any port-forward")

ACCOUNTS = [
    {"_id": "ACC-001", "owner": "Evan Peters",        "balance": 15000, "currency": "PLN"},
    {"_id": "ACC-002", "owner": "Anna Kowalska",         "balance":  3250, "currency": "PLN"},
    {"_id": "ACC-003", "owner": "Firma XYZ sp. z o.o.",  "balance": 42800, "currency": "PLN"},
    {"_id": "ACC-004", "owner": "Piotr Nowak",           "balance":   150, "currency": "PLN"},
]


def main():
    client = connect()
    db = client["bank"]

    inserted = 0
    for acc in ACCOUNTS:
        res = db.accounts.update_one(
            {"_id": acc["_id"]},
            {"$setOnInsert": acc},
            upsert=True,
        )
        if res.upserted_id is not None:
            inserted += 1
            print(f"  + inserted {acc['_id']} ({acc['owner']}, {acc['balance']} {acc['currency']})")
        else:
            print(f"  = kept    {acc['_id']} (already exists)")

    db.transactions.create_index([("createdAt", -1)])

    print(f"\nSeed done. {inserted} accounts inserted, {len(ACCOUNTS)-inserted} already existed.")
    print(f"Total accounts in bank.accounts: {db.accounts.count_documents({})}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
