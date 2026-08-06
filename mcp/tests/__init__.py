# Пакет потрібен, щоб pytest не плутав test_kbtools.py/test_chat.py з
# однойменними модулями в інших test-пакетах репозиторію (admin/tests).
# Сам імпорт kbtools/chat/server (плоский, як у контейнері — mcp/Dockerfile
# копіює mcp/*.py в один /app) робить sys.path.insert(...) на початку кожного
# test_*.py, а не цей файл.
