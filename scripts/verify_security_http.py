"""Read-only checks plus one temporary user-owned conversation against the local application."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.security_client import SecurityClient


def main():
    with SecurityClient("http://127.0.0.1:8000/api/v1",timeout=160) as client:
        user=client.login("admin","rag-e-demo-2026")
        accounts=client.get("/admin/users");accounts.raise_for_status()
        roles=client.get("/admin/roles");roles.raise_for_status()
        bases=client.get("/knowledge/bases");bases.raise_for_status()
        audit=client.get("/admin/audit/verify");audit.raise_for_status();assert audit.json()["valid"]
        request=client.http.build_request("GET","/system")
        client.sign(request,client.access)
        assert client.http.send(request).status_code==200
        assert client.http.send(request).status_code==409
        unsigned=client.http.get("/admin/users",headers={"Authorization":"Bearer "+client.access})
        assert unsigned.status_code==401
        report={"login":"signed","role":user["role"],"permissions":user["permissions"],"accounts":len(accounts.json()),
                "bases":len(bases.json()),"custom_role_endpoint":"ok","audit_chain":audit.json(),"replay":"rejected","unsigned_token":"rejected"}
        (ROOT/"test-results/security-http-final.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":main()
