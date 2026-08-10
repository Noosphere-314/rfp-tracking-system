# Пакет потрібен, щоб pytest поклав у sys.path корінь репозиторію (а не
# worker/tests), інакше `from worker import kb, kb_snapshot, main` у тестах
# не резолвиться — той самий трюк, що й в admin/tests та tgbot/tests.
