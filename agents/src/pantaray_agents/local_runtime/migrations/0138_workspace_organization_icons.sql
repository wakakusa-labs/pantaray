-- An organization's icon, as the data URL the app shows. The app resizes the
-- picked image first; the API accepts only a small PNG, JPEG or WebP.
ALTER TABLE workspace_organizations ADD COLUMN icon_data_url TEXT;
