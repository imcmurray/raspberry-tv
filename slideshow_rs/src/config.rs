//! Handles application configuration loading and management.
//!
//! This module defines the `AppConfig` struct which holds configuration
//! parameters like CouchDB URL, TV UUID, and manager URL. It provides
//! the `load_config` function to read these settings from an INI file.

use configparser::ini::Ini;
use super::errors::ConfigError;
use log::{info, debug, error};

/// Holds the application's configuration parameters.
#[derive(Clone, Debug)]
pub struct AppConfig {
    pub couchdb_url: String,
    pub tv_uuid: String,
    pub manager_url: String,
}

/// Loads application configuration from the specified INI file path.
///
/// Reads settings from the `[settings]` section of the INI file.
///
/// # Arguments
/// * `path` - The path to the configuration file (e.g., "/etc/slideshow.conf").
///
/// # Errors
/// Returns `ConfigError` if the file cannot be read, is malformed,
/// or if essential keys are missing.
#[must_use = "loading configuration can fail, the Result must be handled"]
pub fn load_config(path: &str) -> Result<AppConfig, ConfigError> {
    info!("Attempting to load config from: {}", path);
    let mut config_parser = Ini::new();

    // Load the INI file. Maps the library's error to ConfigError::Parse.
    // Note: If `path` is incorrect and `load` returns an IO-like error,
    // it might be more appropriate to map to `ConfigError::Io`.
    // However, `configparser::ini::Ini::load` typically returns its own error type.
    config_parser.load(path).map_err(|e| {
        error!("Error loading config file '{}': {}", path, e);
        // Attempt to check if it's an IO error by checking the error string.
        // This is a bit fragile. A better way would be if IniError exposed an IO error kind.
        if e.to_string().to_lowercase().contains("os error 2") || // Common "No such file or directory"
           e.to_string().to_lowercase().contains("failed to read file") {
            ConfigError::Io(std::io::Error::new(std::io::ErrorKind::NotFound, e.to_string()))
        } else {
            ConfigError::Parse(e.to_string())
        }
    })?;

    // Helper closure to get a key or return MissingKey error
    let get_key = |key_name: &str| {
        config_parser.get("settings", key_name)
            .ok_or_else(|| {
                error!("Missing configuration key '{}' in section '[settings]' of file '{}'", key_name, path);
                ConfigError::MissingKey(key_name.to_string())
            })
    };

    let couchdb_url = get_key("couchdb_url")?;
    debug!("Loaded config value for key 'couchdb_url': {}", couchdb_url);

    let tv_uuid = get_key("tv_uuid")?;
    debug!("Loaded config value for key 'tv_uuid': {}", tv_uuid);

    let manager_url = get_key("manager_url")?;
    debug!("Loaded config value for key 'manager_url': {}", manager_url);

    let app_config = AppConfig {
        couchdb_url,
        tv_uuid,
        manager_url,
    };
    info!("Configuration loaded successfully from {}: {:?}", path, app_config);
    Ok(app_config)
}
