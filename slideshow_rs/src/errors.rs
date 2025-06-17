//! Defines the custom error types used throughout the `slideshow_rs` application.
//!
//! This module centralizes error handling, providing specific error enums for
//! different categories of issues (configuration, CouchDB interactions, media processing),
//! and a top-level `AppError` to wrap them if needed. Each error type implements
//! `Debug`, `Display`, and `std::error::Error` traits, and provides `From`
//! implementations for common underlying error types.

use std::error::Error as StdError;
use std::fmt;

// --- ConfigError ---
/// Errors related to application configuration loading and parsing.
#[must_use = "a configuration error should be handled or propagated"]
#[derive(Debug)]
pub enum ConfigError {
    /// An I/O error occurred while trying to read the configuration file.
    Io(std::io::Error),
    /// An error occurred while parsing the configuration file content.
    Parse(String),
    /// A required configuration key was missing from the file.
    MissingKey(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Io(e) => write!(f, "Configuration I/O error: {}", e),
            ConfigError::Parse(e) => write!(f, "Configuration parse error: {}", e),
            ConfigError::MissingKey(key) => write!(f, "Missing configuration key: '{}'", key),
        }
    }
}

impl StdError for ConfigError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        match self {
            ConfigError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for ConfigError {
    fn from(err: std::io::Error) -> Self {
        ConfigError::Io(err)
    }
}

// --- CouchDbError ---
/// Errors related to interactions with the CouchDB database.
#[must_use = "a CouchDB error should be handled or propagated"]
#[derive(Debug)]
pub enum CouchDbError {
    /// An error occurred during an HTTP request made by `reqwest`.
    Reqwest(reqwest::Error),
    /// An error occurred during JSON serialization or deserialization.
    SerdeJson(serde_json::Error),
    /// An error occurred while parsing a URL.
    UrlParse(url::ParseError),
    /// A requested CouchDB resource (e.g., document, attachment) was not found.
    NotFound(String),
    /// An HTTP error occurred that was not a simple "Not Found" (e.g., 401, 500).
    HttpError { status: reqwest::StatusCode, message: String },
    /// A generic CouchDB error not covered by other variants.
    Generic(String),
}

impl fmt::Display for CouchDbError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CouchDbError::Reqwest(e) => write!(f, "CouchDB request error: {}", e),
            CouchDbError::SerdeJson(e) => write!(f, "CouchDB JSON (de)serialization error: {}", e),
            CouchDbError::UrlParse(e) => write!(f, "CouchDB URL parse error: {}", e),
            CouchDbError::NotFound(id) => write!(f, "CouchDB resource not found: {}", id),
            CouchDbError::HttpError { status, message } => write!(f, "CouchDB HTTP error {}: {}", status, message),
            CouchDbError::Generic(s) => write!(f, "CouchDB error: {}", s),
        }
    }
}

impl StdError for CouchDbError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        match self {
            CouchDbError::Reqwest(e) => Some(e),
            CouchDbError::SerdeJson(e) => Some(e),
            CouchDbError::UrlParse(e) => Some(e),
            _ => None,
        }
    }
}

impl From<reqwest::Error> for CouchDbError {
    fn from(err: reqwest::Error) -> Self {
        CouchDbError::Reqwest(err)
    }
}

impl From<serde_json::Error> for CouchDbError {
    fn from(err: serde_json::Error) -> Self {
        CouchDbError::SerdeJson(err)
    }
}

impl From<url::ParseError> for CouchDbError {
    fn from(err: url::ParseError) -> Self {
        CouchDbError::UrlParse(err)
    }
}


// --- MediaError ---
/// Errors related to media processing (images, videos, website captures).
#[must_use = "a media error should be handled or propagated"]
#[derive(Debug)]
pub enum MediaError {
    /// An I/O error occurred, often related to temporary files for media.
    Io(std::io::Error),
    /// An error occurred during image processing via the `image` crate.
    Image(image::ImageError),
    /// An error occurred during video processing via `ffmpeg-next`.
    Ffmpeg(ffmpeg_next::Error),
    /// An error occurred during website capture via `headless_chrome`.
    HeadlessChrome(String),
    /// An error occurred when trying to persist a temporary file.
    TempFilePersist(tempfile::PersistError),
    /// An error occurred during the download of media content.
    Download(reqwest::Error),
    /// An error occurred sending a media frame (e.g., video frame) across a channel.
    ChannelSend(String),
    /// A generic media-related error.
    Generic(String),
    /// The format of the media is not supported.
    UnsupportedFormat(String),
}

impl fmt::Display for MediaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MediaError::Io(e) => write!(f, "Media I/O error: {}", e),
            MediaError::Image(e) => write!(f, "Image processing error: {}", e),
            MediaError::Ffmpeg(e) => write!(f, "FFmpeg error: {}", e),
            MediaError::HeadlessChrome(s) => write!(f, "Headless Chrome error: {}", s),
            MediaError::TempFilePersist(e) => write!(f, "Temporary file persistence error: {}", e),
            MediaError::Download(e) => write!(f, "Media download error: {}", e),
            MediaError::ChannelSend(s) => write!(f, "Media frame channel send error: {}", s),
            MediaError::Generic(s) => write!(f, "Media error: {}", s),
            MediaError::UnsupportedFormat(s) => write!(f, "Unsupported media format: {}", s),
        }
    }
}

impl StdError for MediaError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        match self {
            MediaError::Io(e) => Some(e),
            MediaError::Image(e) => Some(e),
            MediaError::Ffmpeg(e) => Some(e),
            MediaError::TempFilePersist(e) => Some(e),
            MediaError::Download(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for MediaError {
    fn from(err: std::io::Error) -> Self { MediaError::Io(err) }
}
impl From<image::ImageError> for MediaError {
    fn from(err: image::ImageError) -> Self { MediaError::Image(err) }
}
impl From<ffmpeg_next::Error> for MediaError {
    fn from(err: ffmpeg_next::Error) -> Self { MediaError::Ffmpeg(err) }
}
impl From<tempfile::PersistError> for MediaError {
    fn from(err: tempfile::PersistError) -> Self { MediaError::TempFilePersist(err) }
}
impl From<reqwest::Error> for MediaError {
    fn from(err: reqwest::Error) -> Self { MediaError::Download(err) }
}


// --- AppError (Top-level error enum) ---
/// A top-level error type that can encompass any error within the application.
#[must_use = "an application error should be handled or propagated"]
#[derive(Debug)]
pub enum AppError {
    Config(ConfigError),
    CouchDb(CouchDbError),
    Media(MediaError),
    Generic(String),
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AppError::Config(e) => write!(f, "Application Configuration Error: {}", e),
            AppError::CouchDb(e) => write!(f, "Application CouchDB Error: {}", e),
            AppError::Media(e) => write!(f, "Application Media Error: {}", e),
            AppError::Generic(s) => write!(f, "Application Error: {}", s),
        }
    }
}

impl StdError for AppError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        match self {
            AppError::Config(e) => Some(e),
            AppError::CouchDb(e) => Some(e),
            AppError::Media(e) => Some(e),
            _ => None,
        }
    }
}

impl From<ConfigError> for AppError {
    fn from(err: ConfigError) -> Self { AppError::Config(err) }
}
impl From<CouchDbError> for AppError {
    fn from(err: CouchDbError) -> Self { AppError::CouchDb(err) }
}
impl From<MediaError> for AppError {
    fn from(err: MediaError) -> Self { AppError::Media(err) }
}
