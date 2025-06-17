//! Handles media fetching, processing, and data structures.
//!
//! This module is responsible for:
//! - Fetching and decoding images from CouchDB attachments.
//! - Capturing website screenshots using a headless browser.
//! - Defining the `VideoData` struct for video playback state (though actual FFmpeg logic is in `main.rs`).

use std::sync::{Arc, Mutex, mpsc as std_mpsc};
use std::thread; // Only used by VideoData if its methods were to spawn threads.
use egui::ColorImage as EguiColorImage;
use tempfile::NamedTempFile; // Used by VideoData for temp file path.
use super::errors::MediaError;
use super::config::AppConfig;
use reqwest::Client as ReqwestClient;

// Headless Chrome
use headless_chrome::{Browser, LaunchOptionsBuilder, error:: κάθετο_error:: κάθετο_Error as HeadlessError, protocol::page::ScreenshotFormat, Viewport};
use image::{ImageFormat, DynamicImage, RgbaImage}; // ImageError is implicitly handled by From<image::ImageError> for MediaError
use std::time::Duration as StdDuration;
use log::{info, error, debug, warn, trace}; // Added trace

/// Holds data related to ongoing video playback.
/// The actual FFmpeg decoding logic is currently in `main.rs` (`SlideshowApp::setup_video_playback`).
#[derive(Debug)] // Added Debug for VideoData
pub struct VideoData {
    /// Receives decoded video frames (or errors) from the decoding thread.
    pub frame_receiver: std_mpsc::Receiver<Result<Arc<EguiColorImage>, MediaError>>,
    /// Guards the temporary file, ensuring it's deleted when `VideoData` is dropped.
    pub _temp_file: NamedTempFile,
    /// Handle for the video decoding thread.
    pub decoder_thread_handle: Option<thread::JoinHandle<()>>,
    /// Flag to signal the decoding thread to stop.
    pub stop_decoder_flag: Arc<Mutex<bool>>,
    /// Identifier for the slide this video data belongs to (e.g., attachment key).
    pub slide_name_key: String,
    /// Width of the video in pixels.
    pub width: u32,
    /// Height of the video in pixels.
    pub height: u32,
}

/// Fetches an image attachment directly from CouchDB and decodes it.
#[must_use = "fetching an image can fail; the Result must be handled"]
pub async fn fetch_image_attachment_direct(config: &AppConfig, client: &ReqwestClient, attachment_name: &str) -> Result<EguiColorImage, MediaError> {
    debug!("Fetching direct image attachment: '{}' for tv_uuid: {}", attachment_name, config.tv_uuid);
    let url = format!("{}/slideshows/{}/{}", config.couchdb_url, config.tv_uuid, attachment_name);

    let response = client.get(&url).send().await.map_err(|e| {
        error!("Request error fetching image attachment '{}' for tv_uuid {}: {:?}", attachment_name, config.tv_uuid, e);
        MediaError::Download(e)
    })?;

    let response = response.error_for_status().map_err(|e| {
        let status = e.status().unwrap_or_default(); // Default if status is somehow None
        error!("HTTP error {} fetching image attachment '{}' for tv_uuid {}: {}", status, attachment_name, config.tv_uuid, e);
        MediaError::Download(e)
    })?;

    let image_bytes = response.bytes().await.map_err(|e| {
        error!("Error reading image bytes for attachment '{}' (tv_uuid {}): {:?}", attachment_name, config.tv_uuid, e);
        MediaError::Download(e)
    })?;

    trace!("Decoding image attachment: {}", attachment_name);
    let img = image::load_from_memory(&image_bytes).map_err(|e| {
        error!("Error decoding image for attachment '{}' (tv_uuid {}): {:?}", attachment_name, config.tv_uuid, e);
        MediaError::Image(e)
    })?;

    let size = [img.width() as _, img.height() as _];
    let image_buffer = img.to_rgba8();
    let pixels = image_buffer.as_flat_samples();
    let egui_image = EguiColorImage::from_rgba_unmultiplied(size, pixels.as_slice());
    info!("Successfully fetched and decoded image attachment: '{}' for tv_uuid {}", attachment_name, config.tv_uuid);
    Ok(egui_image)
}

/// Captures a screenshot of a given URL using a headless Chrome browser.
/// The screenshot is processed to fit a 1920x1080 canvas.
#[must_use = "website screenshot capture can fail; the Result must be handled"]
pub async fn capture_website_screenshot(url_to_capture: String, tv_uuid_for_log: String) -> Result<EguiColorImage, MediaError> {
    info!("Starting website capture for URL: {} (TV: {})", url_to_capture, tv_uuid_for_log);

    let result = tokio::task::spawn_blocking(move || -> Result<EguiColorImage, MediaError> {
        debug!("Preparing headless Chrome for URL: {}", url_to_capture);
        let launch_options = LaunchOptionsBuilder::default()
            .headless(true)
            // Consider adding options like:
            // .no_sandbox(cfg!(target_os = "linux")) // If running as root or in containers
            // .enable_logging(true) // For more verbose browser logs
            .build().map_err(|e: HeadlessError| {
                error!("Failed to build headless Chrome launch options for {}: {}", url_to_capture, e);
                MediaError::HeadlessChrome(e.to_string())
            })?;

        debug!("Launching browser for: {}", url_to_capture);
        let browser = Browser::new(launch_options).map_err(|e: HeadlessError| {
            error!("Failed to launch headless Chrome for {}: {}", url_to_capture, e);
            MediaError::HeadlessChrome(e.to_string())
        })?;

        debug!("Creating new tab for: {}", url_to_capture);
        let tab = browser.new_tab().map_err(|e: HeadlessError| {
            error!("Failed to create new tab for {}: {}", url_to_capture, e);
            MediaError::HeadlessChrome(e.to_string())
        })?;

        debug!("Navigating to: {}", url_to_capture);
        tab.navigate_to(&url_to_capture).map_err(|e: HeadlessError| {
            error!("Failed to navigate to {} in headless Chrome: {}", url_to_capture, e);
            MediaError::HeadlessChrome(e.to_string())
        })?;

        debug!("Waiting for navigation to complete for: {}", url_to_capture);
        // Wait for the page to load, potentially for a specific element or a timeout.
        // tab.wait_for_element_with_custom_timeout("body", Duration::from_secs(30))
        //    .map_err(|e| MediaError::HeadlessChrome(format!("Timeout or error waiting for body: {}", e)))?;
        tab.wait_until_navigated().map_err(|e: HeadlessError| {
            error!("Error waiting for navigation to {} in headless Chrome: {}", url_to_capture, e);
            MediaError::HeadlessChrome(e.to_string())
        })?;

        // A delay can help ensure dynamic content has loaded.
        debug!("Waiting for page render (3s) for: {}", url_to_capture);
        std::thread::sleep(StdDuration::from_secs(3));

        let viewport = Viewport { width: 1920, height: 1080, device_scale_factor: None, ..Default::default() };
        debug!("Setting viewport to 1920x1080 for: {}", url_to_capture);
        tab.set_viewport(viewport).map_err(|e: HeadlessError| {
            error!("Failed to set viewport for {}: {}", url_to_capture, e);
            MediaError::HeadlessChrome(e.to_string())
        })?;

        debug!("Capturing screenshot for: {}", url_to_capture);
        let png_data = tab.capture_screenshot(ScreenshotFormat::PNG, None, None, true) // true for full page if content is taller
            .map_err(|e: HeadlessError| {
                error!("Failed to capture screenshot for {}: {}", url_to_capture, e);
                MediaError::HeadlessChrome(e.to_string())
            })?;

        debug!("Closing tab for: {}", url_to_capture);
        // Ignoring error on tab close as screenshot is already captured.
        if let Err(e) = tab.close(true) {
            warn!("Failed to close tab for {}: {} (non-critical, proceeding with screenshot processing)", url_to_capture, e);
        }

        trace!("Decoding screenshot PNG data for: {}", url_to_capture);
        let mut captured_image = image::load_from_memory_with_format(&png_data, ImageFormat::Png)
            .map_err(|e| {
                error!("Failed to decode screenshot PNG for {}: {}", url_to_capture, e);
                MediaError::Image(e)
            })?;

        let target_width = 1920;
        let target_height = 1080;

        // Process image to fit 1920x1080: crop if larger, paste onto white canvas if smaller.
        if captured_image.width() != target_width || captured_image.height() != target_height {
            debug!("Resizing/cropping screenshot for {} from {}x{} to {}x{}", url_to_capture, captured_image.width(), captured_image.height(), target_width, target_height);
            let mut final_image = RgbaImage::from_pixel(target_width, target_height, image::Rgba([255, 255, 255, 255])); // White background
            // Crop from top-left if source is larger, paste onto canvas.
            let cropped_sub_image = captured_image.crop(0, 0, captured_image.width().min(target_width), captured_image.height().min(target_height));
            image::imageops::overlay(&mut final_image, &cropped_sub_image, 0, 0);
            captured_image = DynamicImage::ImageRgba8(final_image);
        }

        let size = [captured_image.width() as _, captured_image.height() as _];
        let image_buffer = captured_image.to_rgba8();
        let pixels = image_buffer.as_flat_samples();
        let egui_image = EguiColorImage::from_rgba_unmultiplied(size, pixels.as_slice());
        info!("Successfully captured and processed website: {}", url_to_capture);
        Ok(egui_image)

    }).await.map_err(|e| { // Handle JoinError from spawn_blocking
        error!("Tokio task join error for website capture {}: {}", url_to_capture, e);
        MediaError::Generic(format!("Task for {} panicked: {}", url_to_capture, e))
    })?;

    result
}
