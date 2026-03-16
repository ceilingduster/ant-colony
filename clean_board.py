import httpx, os
secret = os.getenv("LIFE_API_SECRET", "")
h = {"X-Life-Secret": secret}
r = httpx.post("http://localhost:8002/queen/write", json={"path": "/data/workspace/.colony_board", "content": "[ADMIN] Colony is at full population. Focus on building complex, ambitious projects. No more replication needed.\n"}, headers=h)
print(r.status_code, r.text)
