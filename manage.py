"""Administration commands.

  python manage.py create-admin     create the Managing Director (or backup admin) account
  python manage.py seed-demo        fill an EMPTY database with demo data for training
"""
import getpass
import sys

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import Permission, User
from app.permissions import AREAS
from app.security import hash_password, password_problems


def create_admin():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    username = input("Username (lower case, e.g. md.sameer): ").strip().lower()
    if db.scalar(select(User).where(User.username == username)):
        sys.exit("That username already exists.")
    full_name = input("Full name: ").strip()
    while True:
        pw = getpass.getpass("Password: ")
        problems = password_problems(pw, username)
        if problems:
            print("Password needs: " + ", ".join(problems))
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            continue
        break
    u = User(username=username, full_name=full_name, password_hash=hash_password(pw), is_superadmin=True,
             must_change_password=False, job_title="Managing Director")
    u.permissions = [Permission(area=a, level="edit") for a in AREAS]
    db.add(u)
    db.commit()
    print(f"Super admin '{username}' created.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create-admin":
        create_admin()
    elif cmd == "seed-demo":
        from app.demo import seed
        seed()
    else:
        print(__doc__)
