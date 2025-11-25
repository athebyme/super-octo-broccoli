-- Добавление полей для авторизации на sexoptovik.ru
-- в таблицу auto_import_settings

ALTER TABLE auto_import_settings
ADD COLUMN IF NOT EXISTS sexoptovik_login VARCHAR(200);

ALTER TABLE auto_import_settings
ADD COLUMN IF NOT EXISTS sexoptovik_password VARCHAR(200);

-- Комментарии для документации
COMMENT ON COLUMN auto_import_settings.sexoptovik_login IS 'Логин для авторизации на sexoptovik.ru (для доступа к фотографиям)';
COMMENT ON COLUMN auto_import_settings.sexoptovik_password IS 'Пароль для авторизации на sexoptovik.ru (для доступа к фотографиям)';
