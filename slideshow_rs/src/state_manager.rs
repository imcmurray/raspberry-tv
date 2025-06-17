//! Manages grouped application states like media caching and text scrolling.
//!
//! This module helps in organizing the `SlideshowApp` state by encapsulating
//! related fields and logic into dedicated manager structs.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Instant, Duration};
use egui_extras::RetainedImage;
use egui::ColorImage as EguiColorImage;
use super::errors::MediaError;
use log::{debug, trace, info};

// Constants for TextScrollState logic
const SCROLL_SPEED_PIXELS_PER_SEC: f32 = 50.0;
const SCROLL_PAUSE_DURATION_SECS: f32 = 2.0;

/// Manages caching of media (images, website screenshots) and tracks pending media fetches.
#[derive(Debug)]
pub struct MediaCacheManager {
    pub image_cache: HashMap<String, RetainedImage>,
    pub website_image_cache: HashMap<String, RetainedImage>,
    /// Stores results of ongoing asynchronous media fetches.
    /// Key is media identifier (URL or attachment name), value is the result.
    pub pending_media: Arc<Mutex<HashMap<String, Result<Arc<EguiColorImage>, MediaError>>>>,
}

impl MediaCacheManager {
    /// Creates a new, empty `MediaCacheManager`.
    pub fn new() -> Self {
        debug!("Initializing new MediaCacheManager.");
        Self {
            image_cache: HashMap::new(),
            website_image_cache: HashMap::new(),
            pending_media: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Clears all media items currently being fetched (pending).
    pub fn clear_all_pending(&mut self) {
        let mut pending_guard = self.pending_media.lock().unwrap();
        if !pending_guard.is_empty() {
            debug!("Clearing {} pending media item(s).", pending_guard.len());
            pending_guard.clear();
        } else {
            trace!("No pending media items to clear.");
        }
    }

    /// Clears all cached images and website screenshots.
    pub fn clear_caches(&mut self) {
        if !self.image_cache.is_empty() || !self.website_image_cache.is_empty() {
            info!("Clearing media caches. Image cache size: {}, Website cache size: {}",
                self.image_cache.len(), self.website_image_cache.len());
            self.image_cache.clear();
            self.website_image_cache.clear();
        } else {
            trace!("Media caches are already empty.");
        }
    }
}

/// Manages the state for scrolling text overlays.
#[derive(Debug)]
pub struct TextScrollState {
    /// Current horizontal scroll offset for each slide (keyed by a unique slide identifier).
    pub offsets: HashMap<String, f32>,
    /// Timestamp of the last scroll update for each slide, for calculating delta time.
    pub last_updates: HashMap<String, Instant>,
    /// Timestamp until which scrolling is paused for a slide (after completing a cycle).
    pub pauses: HashMap<String, Instant>,
}

impl TextScrollState {
    /// Creates a new, empty `TextScrollState`.
    pub fn new() -> Self {
        debug!("Initializing new TextScrollState manager.");
        Self {
            offsets: HashMap::new(),
            last_updates: HashMap::new(),
            pauses: HashMap::new(),
        }
    }

    /// Resets the scroll state for a given slide, typically when it becomes active.
    /// `available_width` is used to set the initial offset for text that should start off-screen.
    pub fn reset_scroll_state_for_slide(&mut self, slide_id_key: &str, available_width: f32) {
        debug!("Resetting scroll state for slide: '{}'. Initial offset will be based on available_width: {}", slide_id_key, available_width);
        // Text starts off-screen to the right if it needs to scroll.
        self.offsets.insert(slide_id_key.to_string(), available_width);
        self.last_updates.insert(slide_id_key.to_string(), Instant::now());
        self.pauses.remove(slide_id_key);
    }

    /// Updates the scroll position for a specific slide based on elapsed time.
    /// This should be called each frame for active scrolling text.
    pub fn update_scroll_for_slide(&mut self, slide_id_key: &str, text_width: f32, available_width: f32, dt: f32) {
        trace!("Updating scroll for slide: '{}'. text_width={}, available_width={}, dt={}", slide_id_key, text_width, available_width, dt);

        // No scrolling needed if text fits or no text.
        if text_width <= available_width || text_width == 0.0 {
            if self.offsets.get(slide_id_key) != Some(&0.0) { // Only update if not already 0 to avoid spamming logs for static text
                trace!("Text for slide '{}' fits or no scroll needed, ensuring offset is 0.", slide_id_key);
                self.offsets.insert(slide_id_key.to_string(), 0.0);
            }
            self.last_updates.insert(slide_id_key.to_string(), Instant::now()); // Still update last_update for correct dt next frame if text changes
            return;
        }

        let now = Instant::now();
        // Check if scrolling is currently paused for this slide
        if self.pauses.get(slide_id_key).map_or(true, |pause_end| now >= *pause_end) {
            // If it was paused and now resuming
            if self.pauses.remove(slide_id_key).is_some() {
                debug!("Resuming scroll for slide: '{}'", slide_id_key);
                // When resuming, ensure dt is based on 'now' not the time before pause.
                self.last_updates.insert(slide_id_key.to_string(), now);
            }

            let current_offset = self.offsets.entry(slide_id_key.to_string()).or_insert(available_width);
            *current_offset -= dt * SCROLL_SPEED_PIXELS_PER_SEC;
            trace!("Scroll updated for slide: '{}', new offset: {}", slide_id_key, *current_offset);

            // If text has fully scrolled past the left edge
            if *current_offset < -text_width {
                debug!("Text scroll completed for slide: '{}'. Resetting to off-screen right and pausing.", slide_id_key);
                *current_offset = available_width; // Reset to start off-screen right
                self.pauses.insert(slide_id_key.to_string(), now + Duration::from_secs_f32(SCROLL_PAUSE_DURATION_SECS));
            }
        } else {
            // Scroll is paused, do nothing to the offset.
            trace!("Scroll for slide '{}' is currently paused.", slide_id_key);
        }
        // Always update last_updates to ensure dt is correct for the next frame,
        // especially important when resuming from a pause.
        self.last_updates.insert(slide_id_key.to_string(), now);
    }

    /// Gets the current horizontal scroll offset for a slide's text.
    /// This value is used when painting the text galley.
    pub fn get_current_offset(&self, slide_id_key: &str, text_width: f32, available_width: f32) -> f32 {
        // If text doesn't need to scroll (it fits), offset is effectively 0 for alignment purposes.
        if text_width <= available_width {
            return 0.0;
        }

        let offset = *self.offsets.get(slide_id_key).unwrap_or(&available_width);
        trace!("Getting current offset for slide '{}': {}", slide_id_key, offset);

        // If paused and the current offset implies it's at the reset position (off-screen right)
        if self.pauses.get(slide_id_key).map_or(false, |pause_end| Instant::now() < *pause_end && offset == available_width) {
             trace!("Scroll for slide '{}' is paused and reset to off-screen right, returning start offset.", slide_id_key);
            return available_width; // Return the starting off-screen position
        }
        offset
    }
}
