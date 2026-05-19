"""
Seed: create Crazy Imagine tenant with its AgentConfig.
Safe to re-run — skips creation if slug already exists.
"""
from sqlalchemy.orm import Session

from app.database import engine
from app import models


def main() -> None:
    with Session(engine) as db:
        existing = db.query(models.Tenant).filter(models.Tenant.slug == "crazy-imagine").first()
        if existing:
            print(f"Tenant already exists: id={existing.id}")
            return

        agent_config = models.AgentConfig(
            name="Crazy Imagine",
            agent_type="generic_info",
        )
        db.add(agent_config)
        db.flush()

        tenant = models.Tenant(
            name="Crazy Imagine",
            slug="crazy-imagine",
            agent_tier=models.AgentTier.support,
            agent_config_id=agent_config.id,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        print(f"Tenant created:")
        print(f"  id             = {tenant.id}")
        print(f"  slug           = {tenant.slug}")
        print(f"  agent_tier     = {tenant.agent_tier}")
        print(f"  agent_config_id= {tenant.agent_config_id}")


if __name__ == "__main__":
    main()
