//! Defines the core data structures and enums used in the slideshow application.
//!
//! This includes models for slides, CouchDB documents, application state, and font assets.
//! These structs are typically deserialized from CouchDB or used to manage internal state.

use serde::Deserialize; // Removed Deserializer as it's not used directly
use serde_alias::serde_alias;
use std::collections::HashMap;
use egui::{FontId, FontFamily};

/// Represents a single slide in the slideshow.
#[derive(Deserialize, Clone, Debug)]
#[serde_alias(type = "type_")] // Allows JSON field "type" to map to struct field "type_"
pub struct Slide {
    /// The name of the slide, often used as an identifier or for display.
    /// For image/video slides from CouchDB attachments, this (or `filename`) is the attachment key.
    pub name: String,
    /// The type of the slide, e.g., "image", "video", "website".
    #[serde(rename = "type")]
    pub type_: String,
    /// Duration in seconds for which the slide should be displayed.
    pub duration: Option<f32>,
    /// Text content for overlay. Can include "{datetime}" for dynamic date/time.
    pub text: Option<String>,
    /// Hex color string for text overlay (e.g., "#FFFFFF").
    pub text_color: Option<String>,
    /// Size of the text overlay (e.g., "small", "medium", "large").
    pub text_size: Option<String>,
    /// Position of the text overlay (e.g., "bottom-center", "top-left").
    pub text_position: Option<String>,
    /// Hex color string for text background (e.g., "#00000080" for semi-transparent black).
    pub text_background_color: Option<String>,
    /// Transition time in seconds (currently not implemented in rendering).
    pub transition_time: Option<f32>,
    /// Flag indicating if the text overlay should scroll if it's too wide.
    #[serde(default)] // Ensures deserialization works if field is missing
    pub scroll_text: Option<bool>,
    /// URL for "website" type slides.
    pub url: Option<String>,
    /// Specific filename for the attachment in CouchDB, if `name` is just a friendly display name.
    /// If this is present and non-empty, it's preferred over `name` for fetching attachments.
    pub filename: Option<String>,
}

/// Information about an attachment in a CouchDB document.
/// Currently minimal, primarily used to check for attachment existence.
#[derive(Deserialize, Clone, Debug)]
pub struct CouchDbAttachmentInfo {
    // Can be expanded with fields like:
    // content_type: String,
    // length: u64,
    // digest: String,
    // stub: bool,
}

/// Represents the main slideshow document fetched from CouchDB.
#[derive(Deserialize, Clone, Debug)]
pub struct SlideshowDocument {
    /// Document ID in CouchDB (typically the TV's UUID).
    pub _id: String,
    /// Document revision in CouchDB.
    pub _rev: String,
    /// List of slides that make up the slideshow.
    pub slides: Option<Vec<Slide>>,
    /// Map of attachment names to their metadata (stubs or full info).
    /// Used for attachment cleanup.
    #[serde(default)]
    pub _attachments: Option<HashMap<String, CouchDbAttachmentInfo>>,
}

/// Holds predefined font identifiers for various text sizes.
#[derive(Clone, Debug)]
pub struct FontAssets {
    pub small: FontId,
    pub medium: FontId,
    pub large: FontId,
}

impl FontAssets {
    /// Creates a new `FontAssets` collection with default font sizes.
    pub fn new() -> Self {
        Self {
            small: FontId::new(14.0, FontFamily::Proportional),
            medium: FontId::new(24.0, FontFamily::Proportional),
            large: FontId::new(36.0, FontFamily::Proportional),
        }
    }

    /// Gets the `FontId` corresponding to a size string (e.g., "small", "large").
    /// Defaults to medium if the size string is None or unrecognized.
    pub fn get_font_id(&self, size_str: Option<&String>) -> FontId {
        match size_str.map(|s| s.to_lowercase()).as_deref() {
            Some("small") => self.small.clone(),
            Some("large") => self.large.clone(),
            _ => self.medium.clone(), // Default to medium
        }
    }
}

/// Represents the overall state of the slideshow application.
/// Used to control UI display and application flow.
#[derive(Clone, Debug, PartialEq)]
pub enum AppState {
    /// Initial state: loading configuration, then fetching slideshow document.
    Connecting,
    /// Actively displaying slideshow content.
    Slideshow,
    /// Displaying a default view (e.g., TV not configured, no slides).
    DefaultView,
    /// An error occurred that prevents normal operation. The String contains the error message.
    Error(String),
}
