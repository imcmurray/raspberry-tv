use eframe::{egui, NativeOptions};
use egui::{CentralPanel, TextureHandle, ColorImage as EguiColorImage, Rect, pos2, vec2, FontId, FontFamily, RichText, Align2, Shape, Color32, TextureOptions, Stroke, Modifiers};
use egui_extras::RetainedImage;
use log::{info, error, warn, debug, trace}; // Added trace
use std::collections::HashMap;
use std::sync::{Arc, Mutex, mpsc as std_mpsc};
use std::thread;
use std::path::PathBuf;
use std::time::{Instant, Duration};
use image::{ImageFormat, guess_format, DynamicImage, ImageOutputFormat, RgbaImage};
use reqwest::Client as ReqwestClient;
use chrono::Local;
use tempfile::NamedTempFile;

use ffmpeg_next as ffmpeg;
use ffmpeg::format::{input, Pixel};
use ffmpeg::media::Type;
use ffmpeg::software::scaling::{context::Context, flag::Flags};
use ffmpeg::util::frame::video::Video;

// Project Modules
mod errors;
mod config;
mod model;
mod couchdb_client;
mod media_pipeline;
mod state_manager;
mod text_renderer;

use errors::*;
use config::*;
use model::*;
use couchdb_client::*;
use media_pipeline::*;
use state_manager::*;
use text_renderer::*;


// --- Constants ---
const CLEANUP_INTERVAL_SECONDS: u64 = 15 * 60;

// --- Communication Keys ---
pub const DOC_FETCH_RESULT_KEY: &str = "app_doc_fetch_result";
pub const VIDEO_FILE_LOADED_KEY: &str = "app_video_file_loaded";


fn get_attachment_key_from_slide(slide: &Slide) -> String {
    slide.filename.as_ref().filter(|s| !s.is_empty()).unwrap_or(&slide.name).clone()
}

struct SlideshowApp {
    app_config: Option<AppConfig>,
    app_state: AppState,
    status_message: String,
    http_client: ReqwestClient,
    slides: Vec<Slide>,
    current_slide_index: usize,
    slide_start_time: Option<Instant>,
    needs_refetch_document: Arc<Mutex<bool>>,
    font_assets: FontAssets,
    egui_ctx: Option<egui::Context>,
    current_video_data: Option<VideoData>,
    current_video_texture: Option<TextureHandle>,
    media_manager: MediaCacheManager,
    scroll_state_manager: TextScrollState,
}

impl SlideshowApp {
    fn new(cc: &eframe::CreationContext<'_>) -> Self {
        info!("Initializing SlideshowApp...");
        ffmpeg::init().expect("Failed to initialize FFmpeg");
        let http_client = ReqwestClient::new();
        let needs_refetch_document_arc = Arc::new(Mutex::new(false));

        let app_config_result = config::load_config("/etc/slideshow.conf");

        let mut initial_app_config = None;
        let mut initial_app_state = model::AppState::Connecting; // Default to connecting
        let mut initial_status_message = "Initializing...".to_string();

        match app_config_result {
            Ok(cfg) => {
                info!("Configuration loaded successfully: {:?}", cfg);
                initial_app_config = Some(cfg.clone());
                initial_app_state = model::AppState::Connecting;
                initial_status_message = "Configuration loaded. Connecting to CouchDB...".to_string();

                let cloned_config_watch = cfg.clone();
                let cloned_needs_refetch_watch = needs_refetch_document_arc.clone();
                let cloned_client_watch = http_client.clone();
                let cloned_ctx_watch = cc.egui_ctx.clone();
                debug!("Spawning CouchDB change watcher task.");
                tokio::spawn(async move {
                    couchdb_client::watch_couchdb_changes(cloned_config_watch, cloned_needs_refetch_watch, cloned_client_watch, cloned_ctx_watch).await;
                });

                let cleanup_config_periodic = cfg.clone();
                let cleanup_client_periodic = http_client.clone();
                debug!("Spawning periodic attachment cleanup task.");
                 tokio::spawn(async move {
                    loop {
                        tokio::time::sleep(Duration::from_secs(CLEANUP_INTERVAL_SECONDS)).await;
                        debug!("Periodic cleanup task waking up.");
                        if let Err(e) = couchdb_client::perform_attachment_cleanup(&cleanup_config_periodic, &cleanup_client_periodic, false).await {
                            error!("Periodic attachment cleanup failed: {}", e);
                        }
                    }
                });
            }
            Err(e) => {
                let err_msg = format!("Failed to load configuration: {}", e);
                error!("{}", err_msg); // Log the full error
                initial_app_config = None;
                initial_app_state = model::AppState::Error(err_msg.clone()); // Store the error message
                initial_status_message = err_msg;
            }
        };

        let app = Self {
            app_config: initial_app_config,
            app_state: initial_app_state,
            status_message: initial_status_message,
            http_client,
            slides: Vec::new(),
            current_slide_index: 0,
            slide_start_time: None,
            needs_refetch_document: needs_refetch_document_arc,
            font_assets: model::FontAssets::new(),
            egui_ctx: Some(cc.egui_ctx.clone()),
            current_video_data: None,
            current_video_texture: None,
            media_manager: MediaCacheManager::new(),
            scroll_state_manager: TextScrollState::new(),
        };

        if app.app_config.is_some() && app.app_state == model::AppState::Connecting {
            info!("Initial state: Connecting. Triggering document fetch.");
            app.trigger_document_fetch(true);
        } else if let model::AppState::Error(ref msg) = app.app_state {
            error!("Application starting in error state: {}", msg);
        }
        app
    }

    fn set_app_state(&mut self, new_state: AppState, message: String) {
        info!("Transitioning AppState from {:?} to {:?}. Message: {}", self.app_state, new_state, message);
        if let AppState::Error(_) = new_state {
            error!("AppState changed to Error: {}", message);
        }
        self.app_state = new_state;
        self.status_message = message;
    }

    fn cleanup_video_resources(&mut self) {
        if let Some(mut video_data) = self.current_video_data.take() {
            info!("Cleaning up video resources for slide: {}", video_data.slide_name_key);
            *video_data.stop_decoder_flag.lock().unwrap() = true;
            if let Some(handle) = video_data.decoder_thread_handle.take() {
                debug!("Joining video decoder thread for {}", video_data.slide_name_key);
                if let Err(e) = handle.join() {
                    error!("Error joining video decoder thread for {}: {:?}", video_data.slide_name_key, e);
                } else {
                    debug!("Successfully joined video decoder thread for {}", video_data.slide_name_key);
                }
            }
        }
        if self.current_video_texture.is_some() {
            debug!("Clearing current video texture.");
            self.current_video_texture = None;
        }
    }

    fn fetch_current_slide_media(&mut self) {
        if self.slides.is_empty() {
            warn!("fetch_current_slide_media called with no slides loaded.");
            return;
        }
        self.cleanup_video_resources();

        let slide = self.slides[self.current_slide_index].clone();
        let media_key = get_attachment_key_from_slide(&slide);
        let url_key = slide.url.clone().unwrap_or_default();
        let slide_id_for_scroll = slide.name.clone();

        debug!("Fetching media for slide (index {}): '{}', type: '{}'", self.current_slide_index, slide.name, slide.type_);
        self.scroll_state_manager.reset_scroll_state_for_slide(&slide_id_for_scroll, 1920.0);

        match slide.type_.to_lowercase().as_str() {
            "image" | "picture" => {
                if !self.media_manager.image_cache.contains_key(&media_key) &&
                   !self.media_manager.pending_media.lock().unwrap().contains_key(&media_key) {
                    info!("Initiating fetch for image slide: '{}', key: '{}'", slide.name, media_key);
                    if let (Some(config), Some(ctx_clone)) = (self.app_config.clone(), self.egui_ctx.clone()) {
                        let client = self.http_client.clone();
                        let pending_media_clone = self.media_manager.pending_media.clone();
                        pending_media_clone.lock().unwrap().entry(media_key.clone()).or_insert_with(|| Err(MediaError::Generic("Loading...".into())));
                        debug!("Spawning task to fetch image attachment: {}", media_key);
                        tokio::spawn(async move {
                            match media_pipeline::fetch_image_attachment_direct(&config, &client, &media_key).await {
                                Ok(img) => { pending_media_clone.lock().unwrap().insert(media_key, Ok(Arc::new(img))); }
                                Err(e) => { pending_media_clone.lock().unwrap().insert(media_key, Err(e)); }
                            }
                            ctx_clone.request_repaint();
                        });
                    }
                } else {
                    debug!("Image slide '{}' (key: '{}') already in cache or pending.", slide.name, media_key);
                }
            }
            "video" => {
                info!("Initiating fetch for video slide: '{}', key: '{}'", slide.name, media_key);
                if let (Some(config), Some(ctx_clone)) = (self.app_config.clone(), self.egui_ctx.clone()) {
                    let client = self.http_client.clone();
                    self.set_app_state(self.app_state.clone(), format!("Loading video: {}", media_key)); // Keep current state, just update message
                    ctx_clone.request_repaint();
                    debug!("Spawning task to fetch video attachment to temp file: {}", media_key);
                    tokio::spawn(async move {
                        match couchdb_client::fetch_attachment_to_temp_file(&config, &client, &media_key).await {
                            Ok(temp_file) => {
                                debug!("Video attachment {} fetched to temp file: {:?}", media_key, temp_file.path());
                                ctx_clone.data_mut(|d| d.insert_persisted(VIDEO_FILE_LOADED_KEY.into(), (media_key.clone(), temp_file.path().to_path_buf(), temp_file)));
                            }
                            Err(e) => {
                                error!("Failed to fetch video attachment {} to temp file: {}", media_key, e);
                                // Send error via context for main thread to handle
                                ctx_clone.data_mut(|d| d.insert_persisted(format!("video_error_{}", media_key).into(), e.to_string()));
                            }
                        }
                        ctx_clone.request_repaint();
                    });
                }
            }
            "website" => {
                if !self.media_manager.website_image_cache.contains_key(&url_key) &&
                   !self.media_manager.pending_media.lock().unwrap().contains_key(&url_key) {
                    info!("Initiating capture for website slide: '{}', URL: '{}'", slide.name, url_key);
                     if let (Some(config), Some(ctx_clone)) = (self.app_config.clone(), self.egui_ctx.clone()) {
                        let pending_media_clone = self.media_manager.pending_media.clone();
                        pending_media_clone.lock().unwrap().entry(url_key.clone()).or_insert_with(|| Err(MediaError::Generic("Capturing...".into())));
                        let tv_uuid = config.tv_uuid.clone();
                        debug!("Spawning task to capture website screenshot: {}", url_key);
                        tokio::spawn(async move {
                            match media_pipeline::capture_website_screenshot(url_key.clone(), tv_uuid).await {
                                Ok(color_image) => { pending_media_clone.lock().unwrap().insert(url_key, Ok(Arc::new(color_image))); }
                                Err(e) => { pending_media_clone.lock().unwrap().insert(url_key, Err(e)); }
                            }
                            ctx_clone.request_repaint();
                        });
                    }
                } else {
                     debug!("Website slide '{}' (URL: '{}') already in cache or pending.", slide.name, url_key);
                }
            }
            _ => { warn!("Unsupported slide type for fetching: '{}' for slide named '{}'", slide.type_, slide.name); }
        }
    }

    fn setup_video_playback(&mut self, slide_name_key: String, temp_file_path: PathBuf, temp_file: NamedTempFile) -> Result<(), MediaError> {
        info!("Setting up video playback for: {}", slide_name_key);
        self.cleanup_video_resources();
        match ffmpeg::format::input(&temp_file_path) {
            Ok(mut ictx) => {
                let input_stream = ictx.streams().best(Type::Video).ok_or_else(|| {
                    error!("FFmpeg: No video stream found for {}", slide_name_key);
                    MediaError::Ffmpeg(ffmpeg::Error::StreamNotFound)
                })?;
                let video_stream_index = input_stream.index();
                debug!("Video stream index for {}: {}", slide_name_key, video_stream_index);

                let context_decoder = ffmpeg::codec::context::Context::from_parameters(input_stream.parameters()).map_err(|e|{
                    error!("FFmpeg: Failed to create decoder context for {}: {:?}", slide_name_key, e);
                    MediaError::Ffmpeg(e)
                })?;
                let mut decoder = context_decoder.decoder().video().map_err(|e|{
                    error!("FFmpeg: Failed to get video decoder for {}: {:?}", slide_name_key, e);
                    MediaError::Ffmpeg(e)
                })?;
                debug!("Video decoder initialized for {}. Format: {:?}, Width: {}, Height: {}", slide_name_key, decoder.format(), decoder.width(), decoder.height());

                let mut scaler = ffmpeg::software::scaling::Context::get(
                    decoder.format(), decoder.width(), decoder.height(),
                    Pixel::RGBA, decoder.width(), decoder.height(), Flags::BILINEAR,
                ).map_err(|e|{
                    error!("FFmpeg: Failed to create scaler for {}: {:?}", slide_name_key, e);
                    MediaError::Ffmpeg(e)
                })?;
                debug!("Video scaler initialized for {}", slide_name_key);

                let (tx_frames, rx_frames) = std_mpsc::sync_channel::<Result<Arc<EguiColorImage>, MediaError>>(5);
                let stop_flag = Arc::new(Mutex::new(false));
                let stop_flag_clone = stop_flag.clone();
                let video_width = decoder.width(); let video_height = decoder.height();
                let slide_name_key_thread = slide_name_key.clone();

                debug!("Spawning video decoding thread for: {}", slide_name_key_thread);
                let thread_handle = thread::spawn(move || {
                    let mut decoded_frame = ffmpeg::util::frame::video::Video::empty();
                    loop {
                        if *stop_flag_clone.lock().unwrap() { info!("Decoder thread for {} received stop signal.", slide_name_key_thread); break; }
                        match ictx.read_frame() {
                            Ok((stream, packet)) => {
                                if stream.index() == video_stream_index {
                                    if let Err(e) = decoder.send_packet(&packet) {
                                        error!("FFmpeg send_packet error for {}: {}", slide_name_key_thread, e);
                                        let _ = tx_frames.send(Err(MediaError::Ffmpeg(e))); break;
                                    }
                                    while decoder.receive_frame(&mut decoded_frame).is_ok() {
                                        let mut rgba_frame = ffmpeg::util::frame::video::Video::empty();
                                        if scaler.run(&decoded_frame, &mut rgba_frame).is_ok() {
                                            let data = rgba_frame.data(0).to_vec();
                                            let color_image = EguiColorImage::from_rgba_unmultiplied([rgba_frame.width() as usize, rgba_frame.height() as usize], &data);
                                            debug!("Sent video frame for: {}", slide_name_key_thread);
                                            if tx_frames.send(Ok(Arc::new(color_image))).is_err() {
                                                warn!("Video frame send failed for {}: receiver dropped.", slide_name_key_thread); break;
                                            }
                                        } else {
                                             error!("FFmpeg frame scaling failed for {}", slide_name_key_thread);
                                             let _ = tx_frames.send(Err(MediaError::Generic("Frame scaling failed".to_string()))); break;
                                        }
                                        thread::sleep(Duration::from_millis(1000 / 60)); // Approx frame pacing
                                    }
                                }
                            }
                            Err(ffmpeg::Error::Eof) => {
                                warn!("Video EOF for {}.", slide_name_key_thread); // Warn because it might be unexpected if slide duration is longer
                                let _ = tx_frames.send(Err(MediaError::Generic("EOF".to_string()))); break;
                            }
                            Err(e) => {
                                error!("FFmpeg frame decoding error for {}: {}", slide_name_key_thread, e);
                                let _ = tx_frames.send(Err(MediaError::Ffmpeg(e))); break;
                            }
                        }
                    }
                    info!("Video decoding thread for {} finished.", slide_name_key_thread);
                });
                self.current_video_data = Some(media_pipeline::VideoData { frame_receiver: rx_frames, _temp_file: temp_file, decoder_thread_handle: Some(thread_handle), stop_decoder_flag: stop_flag, slide_name_key: slide_name_key.clone(), width: video_width, height: video_height });
                self.current_video_texture = None;
                self.set_app_state(self.app_state.clone(), format!("Playing video: {}", slide_name_key));
                Ok(())
            }
            Err(e) => {
                let media_error = MediaError::Ffmpeg(e); // Assuming e is ffmpeg::Error here
                let err_msg = format!("Error opening video file {} for FFmpeg: {}", slide_name_key, media_error);
                error!("{}", err_msg);
                self.set_app_state(AppState::Error(err_msg), format!("Error opening video: {}", slide_name_key));
                Err(media_error)
            }
        }
    }

    fn start_slideshow_from_document(&mut self, doc: SlideshowDocument, immediate_cleanup: bool) {
        info!("Processing new slideshow document: {}, rev: {}", doc._id, doc._rev);
        if let Some(slides_data) = doc.slides {
            if !slides_data.is_empty() {
                debug!("Document has {} slides. Starting slideshow.", slides_data.len());
                self.slides = slides_data;
                self.current_slide_index = 0;
                self.set_app_state(model::AppState::Slideshow, "Slideshow started.".to_string());
                self.slide_start_time = Some(Instant::now());
                self.fetch_current_slide_media();
                self.update_tv_status_for_current_slide();
            } else {
                warn!("No slides found in document {}.", doc._id);
                self.set_app_state(model::AppState::DefaultView, "No slides in document.".to_string());
            }
        } else {
            warn!("Slides array is missing in document {}.", doc._id);
            self.set_app_state(model::AppState::DefaultView, "No slides array in document.".to_string());
        }

        if immediate_cleanup {
            if let Some(config) = self.app_config.clone() {
                let client = self.http_client.clone();
                let doc_for_cleanup = doc.clone();
                debug!("Spawning immediate attachment cleanup task for doc: {}", doc_for_cleanup._id);
                tokio::spawn(async move {
                    if let Err(e) = couchdb_client::perform_attachment_cleanup_with_doc(&config, &client, doc_for_cleanup, true).await {
                        error!("Immediate attachment cleanup failed: {}", e);
                    }
                });
            }
        }
    }

    fn trigger_document_fetch(&mut self, immediate_cleanup_after_fetch: bool) {
        if let (Some(config), Some(ctx_clone)) = (self.app_config.clone(), self.egui_ctx.clone()) {
            let http_client_clone = self.http_client.clone();
            self.set_app_state(model::AppState::Connecting, "Fetching slideshow document...".to_string());
            debug!("Spawning task to fetch document (immediate_cleanup: {})", immediate_cleanup_after_fetch);
            tokio::spawn(async move {
                match couchdb_client::fetch_document_with_attachments(&config, &http_client_clone).await {
                    Ok(doc) => {
                        info!("Document fetched successfully: {}", doc._id);
                        ctx_clone.data_mut(|d| d.insert_persisted(DOC_FETCH_RESULT_KEY.into(), (Ok(doc), immediate_cleanup_after_fetch) ));
                    }
                    Err(e) => {
                        error!("Error in spawned task during document fetch: {}", e);
                        ctx_clone.data_mut(|d| d.insert_persisted(DOC_FETCH_RESULT_KEY.into(), (Err(e), false) ));
                    }
                }
                ctx_clone.request_repaint();
            });
        } else {
            let err_msg = "Cannot trigger document fetch: Configuration not loaded.".to_string();
            error!("{}", err_msg);
            self.set_app_state(model::AppState::Error(err_msg.clone()), err_msg);
        }
    }

    fn update_tv_status_for_current_slide(&self) {
        if self.app_config.is_none() || self.slides.is_empty() { return; }
        let config = self.app_config.as_ref().unwrap().clone();
        let client = self.http_client.clone();
        let slide = self.slides[self.current_slide_index].clone();
        let slide_id = slide.name.clone();
        let slide_filename = match slide.type_.to_lowercase().as_str() {
            "website" => slide.url.clone().unwrap_or_default(),
            _ => get_attachment_key_from_slide(&slide),
        };
        debug!("Spawning task to update TV status for slide: {}", slide_id);
        tokio::spawn(async move {
            if let Err(e) = couchdb_client::update_tv_status(&config, &client, &slide_id, &slide_filename).await {
                warn!("Failed to update TV status for slide {}: {}", slide_id, e);
            }
        });
     }
}

impl eframe::App for SlideshowApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.egui_ctx.is_none() { self.egui_ctx = Some(ctx.clone()); }

        // Handle video file loaded from async task
        if let Some((key, path, guard)) = ctx.data_mut(|d| d.remove_persisted::<(String, PathBuf, NamedTempFile)>(VIDEO_FILE_LOADED_KEY.into())) {
            info!("Processing loaded video file for key: {}", key);
            if let Err(e) = self.setup_video_playback(key.clone(), path, guard) {
                let err_msg = format!("Error setting up video playback for {}: {}", key, e);
                // self.set_app_state(AppState::Error(err_msg.clone()), err_msg); // This might be too abrupt
                error!("{}", err_msg); // Already logged in setup_video_playback
                self.status_message = err_msg; // Show error on screen
            }
        }
        // Handle generic video error key that might have been set by fetch_attachment_to_temp_file
        // This is a bit clunky; ideally all errors come through pending_media or specific typed results.
        // For now, checking if any such key exists (though it should be specific to a media_key)
        // This might be an outdated pattern after error handling improvements.
        // Example: if let Some(e_str) = ctx.data_mut(|d| d.remove_persisted::<String>(format!("video_error_{}", "some_key").into())) { ... }
        // This part is tricky as we don't know "some_key" here. Best to ensure errors are propagated through typed results.

        let mut needs_refetch_now = false;
        if let Ok(mut guard) = self.needs_refetch_document.lock() { if *guard { *guard = false; needs_refetch_now = true; }}
        if needs_refetch_now {
            info!("Document refetch triggered by watcher.");
            self.cleanup_video_resources();
            self.set_app_state(model::AppState::Connecting, "Configuration changed. Refetching...".to_string());
            self.slides.clear();
            self.media_manager.clear_caches();
            self.media_manager.clear_all_pending();
            self.trigger_document_fetch(true);
        }

        if self.app_state == model::AppState::Connecting && self.app_config.is_some() {
             if let Some((result, immediate_cleanup)) = ctx.data_mut(|d| d.remove_persisted::<(Result<SlideshowDocument, CouchDbError>, bool)>(DOC_FETCH_RESULT_KEY.into())) {
                info!("Processing document fetch result.");
                match result {
                    Ok(doc) => {
                        info!("Document {} fetched successfully.", doc._id);
                        self.start_slideshow_from_document(doc, immediate_cleanup);
                    }
                    Err(e) => {
                        let err_msg = format!("Error fetching document: {}", e);
                        error!("{}", err_msg);
                        match e {
                            CouchDbError::NotFound(id) => {
                                let specific_msg = if let Some(cfg) = &self.app_config {
                                     format!("This TV (UUID: {}) is not configured (doc ID {} not found). Please add it to the slideshow manager at {}.", cfg.tv_uuid, id, cfg.manager_url)
                                } else {
                                     format!("TV configuration document {} not found.", id)
                                };
                                self.set_app_state(model::AppState::DefaultView, specific_msg);
                            }
                            _ => {
                                self.set_app_state(model::AppState::Error(err_msg), e.to_string());
                            }
                        }
                    }
                }
            }
        }

        let mut new_media_to_cache = Vec::new();
        self.media_manager.pending_media.lock().unwrap().retain(|key, result_arc_media_res| {
            match result_arc_media_res {
                Ok(arc_media) => {
                    info!("Media successfully fetched for key: {}", key);
                    new_media_to_cache.push((key.clone(), arc_media.clone()));
                    false // Remove from pending
                }
                Err(media_err) => {
                    // Don't log "Loading..." or "Capturing..." as errors here, they are transient states.
                    if !media_err.to_string().contains("Loading...") && !media_err.to_string().contains("Capturing...") {
                        error!("Failed to fetch media for key {}: {}", key, media_err);
                    }
                    false // Remove actual errors from pending
                }
            }
        });

        for (key, arc_color_image) in new_media_to_cache {
            let is_website = key.starts_with("http://") || key.starts_with("https://");
            let retained_image = RetainedImage::from_color_image(key.clone(), (*arc_color_image).clone());
            if is_website {
                debug!("Caching website screenshot for URL: {}", key);
                self.media_manager.website_image_cache.insert(key, retained_image);
            } else {
                debug!("Caching image for key: {}", key);
                self.media_manager.image_cache.insert(key, retained_image);
            }
        }

        if self.app_state == model::AppState::Slideshow {
            ctx.request_repaint_after(Duration::from_millis(1000/30)); // Aim for ~30fps for UI responsiveness & video
            if let Some(start_time) = self.slide_start_time {
                if self.slides.is_empty() {
                    warn!("In Slideshow state but no slides available. Switching to DefaultView.");
                    self.set_app_state(model::AppState::DefaultView, "No slides available".to_string());
                    return;
                }
                let current_slide_data = &self.slides[self.current_slide_index];
                let duration_secs = current_slide_data.duration.unwrap_or(10.0);
                if start_time.elapsed() > Duration::from_secs_f32(duration_secs) {
                    let old_slide_index = self.current_slide_index;
                    self.current_slide_index = (self.current_slide_index + 1) % self.slides.len();
                    self.slide_start_time = Some(Instant::now());
                    info!("Advancing slide from index {} to {}: '{}', type: '{}'",
                        old_slide_index, self.current_slide_index,
                        self.slides[self.current_slide_index].name,
                        self.slides[self.current_slide_index].type_
                    );
                    self.fetch_current_slide_media();
                    self.update_tv_status_for_current_slide();
                }
            } else { // Should not happen if in Slideshow state
                warn!("In Slideshow state but slide_start_time is None. Resetting.");
                self.slide_start_time = Some(Instant::now());
            }
        }

        CentralPanel::default().show(ctx, |ui| {
            match &self.app_state {
                model::AppState::Connecting => { ui.centered_and_justified(|ui| ui.label(&self.status_message)); }
                model::AppState::Slideshow => {
                    if self.slides.is_empty() { ui.centered_and_justified(|ui| ui.label("No slides.")); return; }
                    let slide = self.slides[self.current_slide_index].clone();
                    let available_rect = ui.available_rect_before_wrap();
                    let slide_id_for_scroll = slide.name.clone();

                    if slide.scroll_text.unwrap_or(false) {
                        let galley_placeholder = ui.painter().layout_no_wrap(
                            slide.text.clone().unwrap_or_default().replace("{datetime}", &Local::now().format("%Y-%m-%d %H:%M:%S").to_string()),
                            self.font_assets.get_font_id(slide.text_size.as_ref()),
                            Color32::TRANSPARENT // Color doesn't matter for layout size
                        );
                        let text_box_width = available_rect.width() - 2.0 * 10.0; // Approx padding for text box
                        let dt = self.scroll_state_manager.last_updates.get(&slide_id_for_scroll)
                            .map_or(0.0, |lu| Instant::now().duration_since(*lu).as_secs_f32());

                        trace!("Updating scroll for slide '{}': text_width={}, box_width={}, dt={}", slide_id_for_scroll, galley_placeholder.size().x, text_box_width, dt);
                        self.scroll_state_manager.update_scroll_for_slide(&slide_id_for_scroll, galley_placeholder.size().x, text_box_width, dt);
                    }

                    let (style, layout) = text_renderer::parse_slide_text_properties(&slide, &self.font_assets);

                    match slide.type_.to_lowercase().as_str() {
                        "image" | "picture" => {
                            let image_key = get_attachment_key_from_slide(&slide);
                            if let Some(retained_image) = self.media_manager.image_cache.get_mut(&image_key) {
                                let img_rect = calculate_draw_rect(retained_image.width() as f32, retained_image.height() as f32, available_rect);
                                retained_image.show_rect(ui, img_rect);
                                text_renderer::draw_text_overlay(ui, img_rect, &slide_id_for_scroll, &style, &layout, &self.scroll_state_manager);
                            } else { ui.centered_and_justified(|ui| ui.label(if self.media_manager.pending_media.lock().unwrap().contains_key(&image_key) { "Loading image..." } else { "Image not found." })); }
                        }
                        "website" => {
                            let url_key = slide.url.as_ref().unwrap_or(&String::new()).clone();
                             if let Some(retained_image) = self.media_manager.website_image_cache.get_mut(&url_key) {
                                let img_rect = calculate_draw_rect(retained_image.width() as f32, retained_image.height() as f32, available_rect);
                                retained_image.show_rect(ui, img_rect);
                                text_renderer::draw_text_overlay(ui, img_rect, &slide_id_for_scroll, &style, &layout, &self.scroll_state_manager);
                            } else { ui.centered_and_justified(|ui| ui.label(if self.media_manager.pending_media.lock().unwrap().contains_key(&url_key) { "Capturing website..." } else { "Website screenshot not found." })); }
                        }
                        "video" => {
                            if let Some(video_data) = &mut self.current_video_data {
                                let video_key = get_attachment_key_from_slide(&slide);
                                if video_data.slide_name_key == video_key {
                                    if let Ok(frame_result) = video_data.frame_receiver.try_recv() {
                                        match frame_result {
                                            Ok(frame_arc) => {
                                                debug!("Received video frame for {}", video_key);
                                                if let Some(tex) = &mut self.current_video_texture { tex.set((*frame_arc).clone(), TextureOptions::LINEAR); }
                                                else { self.current_video_texture = Some(ctx.load_texture(&format!("vid_{}", video_key), (*frame_arc).clone(), TextureOptions::LINEAR)); }
                                            }
                                            Err(e) => {
                                                match e {
                                                    MediaError::Generic(ref s) if s == "EOF" => warn!("Video EOF for {}. Last frame will be shown.", video_key),
                                                    _ => error!("Video frame error for {}: {}", video_key, e),
                                                }
                                                // self.status_message = format!("Video error: {}", e); // Could be too noisy
                                            }
                                        }
                                    }
                                    if let Some(tex) = &self.current_video_texture {
                                        let vid_rect = calculate_draw_rect(video_data.width as f32, video_data.height as f32, available_rect);
                                        let mut image_job = egui::widgets::Image::new(tex, vid_rect.size());
                                        ui.allocate_ui_at_rect(vid_rect, |ui_image| { ui_image.add(image_job); });
                                        text_renderer::draw_text_overlay(ui, vid_rect, &slide_id_for_scroll, &style, &layout, &self.scroll_state_manager);
                                    } else { ui.centered_and_justified(|ui| ui.label("Loading video frame...")); }
                                } else {
                                    warn!("Video data mismatch. Expected key {}, found {}. Refetching.", video_key, video_data.slide_name_key);
                                    self.fetch_current_slide_media();
                                }
                            } else { ui.centered_and_justified(|ui| ui.label("Loading video...")); }
                        }
                        _ => { ui.centered_and_justified(|ui| ui.label(format!("Unsupported slide type: {}", slide.type_))); }
                    }
                }
                model::AppState::DefaultView => { ui.centered_and_justified(|ui| ui.label(&self.status_message)); }
                model::AppState::Error(e) => { ui.centered_and_justified(|ui| { ui.colored_label(Color32::RED, format!("Error: {}", e)); }); }
            }
        });
    }
    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        info!("SlideshowApp on_exit called. Cleaning up video resources.");
        self.cleanup_video_resources();
    }
}

fn calculate_draw_rect(media_width: f32, media_height: f32, available_rect: Rect) -> Rect {
    let aspect_ratio = media_width / media_height;
    let mut draw_width = available_rect.width();
    let mut draw_height = available_rect.width() / aspect_ratio;
    if draw_height > available_rect.height() {
        draw_height = available_rect.height();
        draw_width = available_rect.height() * aspect_ratio;
    }
    Rect::from_center_size(available_rect.center(), vec2(draw_width, draw_height))
}

#[tokio::main]
async fn main() -> Result<(), eframe::Error> {
    env_logger::init(); // Initialize logger
    info!("Starting slideshow_rs application...");
    let options = NativeOptions { initial_window_size: Some(egui::vec2(1024.0, 768.0)), fullscreen: false, ..Default::default() };
    eframe::run_native("Slideshow RS", options, Box::new(|cc| Box::new(SlideshowApp::new(cc))))
}

[end of slideshow_rs/src/main.rs]
