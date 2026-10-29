from . import config
from .engine import Engine
from .runtime import RuntimeStore

def main():
    RuntimeStore(config.DATABASE_URL)
    Engine(config.DATABASE_URL)
    Engine(config.DEMO_DATABASE_URL)
    print('Database initialized. Live execution remains disabled.')
if __name__=='__main__':main()
