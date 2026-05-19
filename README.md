# minibots API

Run the application with
```bash
uv run fastapi dev app/main.py
```

## Local infrastructure

```bash
docker compose up -d
```

## Garage S3 — first-time setup

After starting the containers for the first time, initialize Garage:

**1. Get the node ID**
```bash
docker compose exec garage /garage status
```

**2. Assign a layout to the node**
```bash
docker compose exec garage /garage layout assign -z local -c 1G <node-id>
```

**3. Apply the layout**
```bash
docker compose exec garage /garage layout apply --version 1
```

**4. Create a bucket**
```bash
docker compose exec garage /garage bucket create minibots
```

**5. Create an access key**
```bash
docker compose exec garage /garage key create minibots-key
```

**6. Grant the key access to the bucket**
```bash
docker compose exec garage /garage bucket allow --read --write --owner minibots --key minibots-key
```

Copy the `Key ID` and `Secret key` from step 5 into your `.env`:

```env
GARAGE_ENDPOINT=http://localhost:3900
GARAGE_REGION=garage
GARAGE_ACCESS_KEY_ID=<Key ID>
GARAGE_SECRET_ACCESS_KEY=<Secret key>
GARAGE_BUCKET=minibots
```
