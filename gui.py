#!/usr/bin/env python3
"""
CLIP Image Search GUI

A simple graphical user interface for searching images using CLIP embeddings.
Supports both file-based and text-based search queries.
"""

import os
import json
import sys
import numpy as np
import torch
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from typing import Dict, List, Tuple, Any, Optional
import threading
from pathlib import Path
import tempfile
from tqdm import tqdm
from transformers import CLIPProcessor, CLIPModel, CLIPTokenizer
import webbrowser
import zstandard as zstd  # Add this import at the top
import utils
import time
import generate  # Import the generate module for batch processing functions
import numpy as np
import hashlib

class CLIPSearchApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CLIP Image Search")
        self.root.geometry("1500x1000")
        self.root.minsize(800, 600)

        # Set app icon if available
        try:
            self.root.iconbitmap("search_icon.ico")
        except:
            pass

        # Variables
        self.embeddings_file = tk.StringVar()
        self.query_image = tk.StringVar()
        self.query_text = tk.StringVar()
        self.num_results = tk.IntVar(value=90)
        self.model_name = tk.StringVar(value="laion/CLIP-ViT-H-14-laion2B-s32B-b79K")
        self.status_text = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)
        self.gen_stop_flag = False

        # Model and embeddings
        self.model = None
        self.processor = None
        self.tokenizer = None
        self.embeddings = {}
        self.result_paths = []
        self.current_page = 0
        self.results_per_page = 30
        self.thumbnails = []

        # Add cache for search results
        self.cached_results = []  # Store full search results

        # Add thumbnail cache and persistence
        self.thumbnail_cache = {}  # Store PhotoImage objects by path
        self.thumbnail_load_queue = []  # Queue for lazy loading
        self.is_loading_thumbnails = False
        self.thumbnail_dir = os.path.join(os.path.expanduser("~"), ".clip_search", "thumbnails")
        os.makedirs(self.thumbnail_dir, exist_ok=True)

        # Adjust default threshold to be more strict
        self.min_score = 0.0  # Minimum similarity score to display
        self.auto_threshold = tk.BooleanVar(value=True)  # Whether to auto-adjust threshold
        self.manual_threshold = tk.DoubleVar(value=0.25)  # Higher default manual threshold

        # Create UI elements
        self._create_menu()
        self._create_layout()

        # Bind events
        self.query_text_entry.bind("<Return>", self._on_text_search)

        # Load last session if available
        self._load_config()

    def _create_menu(self):
        menubar = tk.Menu(self.root)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open Embeddings File", command=self._browse_embeddings)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        # Search menu
        search_menu = tk.Menu(menubar, tearoff=0)
        search_menu.add_command(label="Search by Image", command=self._browse_query_image)
        search_menu.add_command(label="Search by Text", command=lambda: self.query_text_entry.focus_set())
        search_menu.add_separator()
        search_menu.add_command(label="Clear Results", command=self._clear_results)
        menubar.add_cascade(label="Search", menu=search_menu)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _create_layout(self):
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Create a notebook (tabbed interface)
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Create the search tab
        search_tab = ttk.Frame(self.notebook)
        self.notebook.add(search_tab, text="Search")
        
        # Create the generate tab
        generate_tab = ttk.Frame(self.notebook)
        self.notebook.add(generate_tab, text="Generate Embeddings")

        # Add search controls to search tab
        self._create_search_tab(search_tab)
        
        # Create generate tab content
        self._create_generate_tab(generate_tab)
        
        # Pagination frame
        self.pagination_frame = ttk.Frame(main_frame)
        self.pagination_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(self.pagination_frame, text="Previous", command=self._prev_page).pack(side=tk.LEFT)
        self.page_label = ttk.Label(self.pagination_frame, text="Page 1")
        self.page_label.pack(side=tk.LEFT, padx=10)
        ttk.Button(self.pagination_frame, text="Next", command=self._next_page).pack(side=tk.LEFT)

        # Status bar
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.progress_bar = ttk.Progressbar(status_frame, variable=self.progress_var, length=200, mode="determinate")
        self.progress_bar.pack(side=tk.RIGHT, padx=10, pady=5)

        status_label = ttk.Label(status_frame, textvariable=self.status_text)
        status_label.pack(side=tk.LEFT, padx=10, pady=5)
        
        # Bind window resize event
        self.root.bind("<Configure>", self._on_window_resize)

    def _create_search_tab(self, parent):
        # Top section (configuration and search controls)
        top_frame = ttk.Frame(parent)
        top_frame.pack(fill=tk.X, pady=(0, 10))

        # Embeddings & Directory settings
        settings_frame = ttk.LabelFrame(top_frame, text="Settings", padding="5")
        settings_frame.pack(fill=tk.X, side=tk.LEFT, expand=True)

        ttk.Label(settings_frame, text="Embeddings:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(settings_frame, textvariable=self.embeddings_file, width=40).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(settings_frame, text="Browse...", command=self._browse_embeddings).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(settings_frame, text="Model:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        model_entry = ttk.Entry(settings_frame, textvariable=self.model_name, width=40)
        model_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(settings_frame, text="Load", command=self._load_model).grid(row=1, column=2, padx=5, pady=5)

        ttk.Button(settings_frame, text="Load Embeddings", command=self._load_embeddings).grid(row=2, column=0, columnspan=3, padx=5, pady=5, sticky=tk.EW)

        settings_frame.columnconfigure(1, weight=1)

        # Search controls
        search_frame = ttk.LabelFrame(top_frame, text="Search", padding="5")
        search_frame.pack(fill=tk.X, side=tk.RIGHT, expand=True, padx=(10, 0))

        ttk.Label(search_frame, text="Text Query:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.query_text_entry = ttk.Entry(search_frame, textvariable=self.query_text, width=30)
        self.query_text_entry.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(search_frame, text="Search", command=self._on_text_search).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(search_frame, text="Image Query:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(search_frame, textvariable=self.query_image, width=30).grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(search_frame, text="Browse...", command=self._browse_query_image).grid(row=1, column=2, padx=5, pady=5)

        ttk.Label(search_frame, text="Results:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Spinbox(search_frame, from_=1, to=100, textvariable=self.num_results, width=5).grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)

        # Add threshold controls to search frame
        ttk.Label(search_frame, text="Score Threshold:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        threshold_frame = ttk.Frame(search_frame)
        threshold_frame.grid(row=3, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=5)
        
        ttk.Checkbutton(threshold_frame, text="Auto", variable=self.auto_threshold).pack(side=tk.LEFT)
        self.threshold_spinbox = ttk.Spinbox(threshold_frame, from_=0.0, to=1.0, increment=0.05,
                                           textvariable=self.manual_threshold, width=5)
        self.threshold_spinbox.pack(side=tk.LEFT, padx=5)
        
        # Move Clear Results button to row 4
        ttk.Button(search_frame, text="Clear Results", command=self._clear_results).grid(
            row=4, column=0, columnspan=3, padx=5, pady=5, sticky=tk.EW)

        search_frame.columnconfigure(1, weight=1)

        # Results section
        results_frame = ttk.LabelFrame(parent, text="Search Results", padding="5")
        results_frame.pack(fill=tk.BOTH, expand=True)

        # Canvas for displaying image results with scrolling
        self.canvas_frame = ttk.Frame(results_frame)
        self.canvas_frame.pack(fill=tk.BOTH, expand=True)

        # Canvas and scrollbar
        self.canvas = tk.Canvas(self.canvas_frame, bg="white")
        self.scrollbar = ttk.Scrollbar(self.canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Frame inside canvas for images
        self.results_container = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.results_container, anchor=tk.NW)

        # Configure canvas scrolling
        self.results_container.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _create_generate_tab(self, parent):
        # Variables for embedding generation
        self.gen_directory = tk.StringVar()
        self.gen_output = tk.StringVar(value=self.embeddings_file.get())  # Initialize with current embeddings
        self.gen_model = tk.StringVar(value=self.model_name.get())
        self.gen_batch_size = tk.IntVar(value=16)
        self.gen_fp16 = tk.BooleanVar(value=False)
        self.gen_progress = tk.DoubleVar(value=0)
        self.gen_status = tk.StringVar(value="Ready")
        
        # Main frame
        frame = ttk.Frame(parent, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Input settings
        settings_frame = ttk.LabelFrame(frame, text="Generation Settings", padding="10")
        settings_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Image directory
        ttk.Label(settings_frame, text="Images Directory:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(settings_frame, textvariable=self.gen_directory, width=40).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(settings_frame, text="Browse...", command=self._browse_gen_directory).grid(row=0, column=2, padx=5, pady=5)
        
        # Output file
        ttk.Label(settings_frame, text="Output File:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(settings_frame, textvariable=self.gen_output, width=40).grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(settings_frame, text="Browse...", command=self._browse_gen_output).grid(row=1, column=2, padx=5, pady=5)
        
        # Use current embeddings button
        ttk.Button(settings_frame, text="Use Current Embeddings File", 
                   command=lambda: self.gen_output.set(self.embeddings_file.get())).grid(
            row=1, column=3, padx=5, pady=5)
        
        # Model
        ttk.Label(settings_frame, text="Model:").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(settings_frame, textvariable=self.gen_model, width=40).grid(row=2, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(settings_frame, text="Use Search Model", command=lambda: self.gen_model.set(self.model_name.get())).grid(
            row=2, column=2, padx=5, pady=5)
        
        # Batch size
        ttk.Label(settings_frame, text="Batch Size:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Spinbox(settings_frame, from_=1, to=64, textvariable=self.gen_batch_size, width=5).grid(
            row=3, column=1, sticky=tk.W, padx=5, pady=5)
        
        # FP16 checkbox
        ttk.Checkbutton(settings_frame, text="Use FP16 Precision (faster & uses less VRAM, but less accurate) (not recommended, use smaller model instead)", variable=self.gen_fp16).grid(
            row=4, column=0, columnspan=3, sticky=tk.W, padx=5, pady=5)
        
        # Generate button
        ttk.Button(settings_frame, text="Generate Embeddings", command=self._generate_embeddings).grid(
            row=5, column=0, columnspan=3, padx=5, pady=10, sticky=tk.EW)
        
        settings_frame.columnconfigure(1, weight=1)
        
        # Progress section
        progress_frame = ttk.LabelFrame(frame, text="Progress", padding="10")
        progress_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # Progress bar
        self.gen_progress_bar = ttk.Progressbar(progress_frame, variable=self.gen_progress, length=200, mode="determinate")
        self.gen_progress_bar.pack(fill=tk.X, padx=5, pady=5)
        
        # Status label
        ttk.Label(progress_frame, textvariable=self.gen_status).pack(fill=tk.X, padx=5, pady=5)
        
        # Log frame with text widget
        log_frame = ttk.LabelFrame(frame, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        # Text widget with scrollbar
        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.log_text = tk.Text(log_frame, height=10, width=50, wrap=tk.WORD, yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.config(command=self.log_text.yview)

    def _browse_embeddings(self):
        filename = filedialog.askopenfilename(
            title="Select Embeddings File",
            filetypes=(
                ("Compressed JSON", "*.json.zst"),
                ("JSON files", "*.json"),
                ("All files", "*.*")
            )
        )
        if filename:
            self.embeddings_file.set(filename)
            self._save_config()

    def _browse_query_image(self):
        filename = filedialog.askopenfilename(
            title="Select Query Image",
            filetypes=(
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.webp"),
                ("All files", "*.*")
            )
        )
        if filename:
            self.query_image.set(filename)
            self._image_search()

    def _load_model(self):
        def load_task():
            try:
                model_name = self.model_name.get()
                self.status_text.set(f"Loading model {model_name}...")

                # Load model components
                self.tokenizer = CLIPTokenizer.from_pretrained(model_name)
                self.processor = CLIPProcessor.from_pretrained(model_name)

                # Load model with CUDA if available
                if torch.cuda.is_available():
                    device = "cuda"
                    self.model = CLIPModel.from_pretrained(model_name).to(device)
                    self.status_text.set(f"Model loaded on GPU")
                else:
                    device = "cpu"
                    self.model = CLIPModel.from_pretrained(model_name)
                    self.status_text.set(f"Model loaded on CPU")
            except Exception as e:
                self.status_text.set(f"Error loading model: {str(e)}")
                messagebox.showerror("Error", f"Failed to load model: {str(e)}")

        threading.Thread(target=load_task, daemon=True).start()

    def _load_embeddings(self):
        if not os.path.isfile(self.embeddings_file.get()):
            messagebox.showerror("Error", "Please select a valid embeddings file")
            return

        def load_task():
            try:
                self.status_text.set("Loading embeddings...")
                self.progress_var.set(0)

                # Handle both .json and .zst files
                filepath = self.embeddings_file.get()
                if filepath.endswith('.zst'):
                    with open(filepath, 'rb') as f:
                        dctx = zstd.ZstdDecompressor()
                        json_str = dctx.decompress(f.read()).decode('utf-8')
                        self.embeddings = json.loads(json_str)
                else:
                    # Legacy JSON support
                    with open(filepath, 'r') as f:
                        self.embeddings = json.load(f)

                self.progress_var.set(100)
                self.status_text.set(f"Loaded {len(self.embeddings)} embeddings")
            except Exception as e:
                self.status_text.set(f"Error loading embeddings: {str(e)}")
                messagebox.showerror("Error", f"Failed to load embeddings: {str(e)}")

        threading.Thread(target=load_task, daemon=True).start()

    def _on_text_search(self, event=None):
        self._text_search()

    def _text_search(self):
        if not self.query_text.get().strip():
            messagebox.showinfo("Info", "Please enter a text query")
            return

        if not self.model or not self.tokenizer:
            messagebox.showinfo("Info", "Please load a model first")
            return

        if not self.embeddings:
            messagebox.showinfo("Info", "Please load embeddings first")
            return

        def search_task():
            try:
                query = self.query_text.get().strip()
                self.status_text.set(f"Searching for '{query}'...")
                self.progress_var.set(10)

                # Generate text embedding
                inputs = self.tokenizer([query], padding=True, return_tensors="pt")

                if torch.cuda.is_available():
                    inputs = {k: v.cuda() for k, v in inputs.items()}

                with torch.no_grad():
                    text_features = self.model.get_text_features(**inputs)
                    text_embedding = text_features.cpu().numpy()[0]
                    text_embedding = text_embedding / np.linalg.norm(text_embedding)

                self.progress_var.set(50)

                # Search for similar images
                similarities = []
                for path, data in self.embeddings.items():
                    if 'embedding' not in data:
                        continue

                    image_embedding = np.array(data['embedding'])
                    image_embedding = image_embedding / np.linalg.norm(image_embedding)
                    similarity = np.dot(text_embedding, image_embedding)
                    similarities.append((path, similarity))

                # Sort by similarity
                similarities.sort(key=lambda x: x[1], reverse=True)

                self.progress_var.set(90)

                # Store all results in cache
                self.cached_results = similarities
                
                # Update display with current page and threshold
                self._update_displayed_results()
                
                self.progress_var.set(100)
                
            except Exception as e:
                self.status_text.set(f"Error during search: {str(e)}")
                messagebox.showerror("Error", f"Search failed: {str(e)}")

        threading.Thread(target=search_task, daemon=True).start()

    def _update_displayed_results(self):
        """Update the result_paths based on threshold and sorting"""
        if not self.cached_results:
            return
        
        # Get threshold value
        if self.auto_threshold.get():
            # Calculate dynamic threshold based on score distribution
            scores = [score for _, score in self.cached_results]
            if scores:
                # Use more aggressive thresholding
                mean_score = sum(scores) / len(scores)
                std_score = np.std(scores)
                
                # Options for stricter thresholding:
                # 1. Use mean + percentage of range
                score_range = max(scores) - min(scores)
                threshold_from_range = mean_score + (score_range * 0.2)  # Take top 5% of range (increased from 0.1)
                
                # 2. Use higher percentile
                percentile_95 = np.percentile(scores, 95)  # Only show top 5% of scores (increased from 90)
                
                # 3. Use mean + std with larger multiplier
                threshold_from_std = mean_score + (1.0 * std_score)  # More strict (increased from 0.5)
                
                # Take the highest value of our three approaches
                self.min_score = max(threshold_from_range, percentile_95, threshold_from_std)
                
                # Ensure minimum threshold
                self.min_score = max(0.3, self.min_score)  # Higher minimum threshold (increased from 0.25)
                
                # Update manual threshold for display
                self.manual_threshold.set(round(self.min_score, 2))
        else:
            self.min_score = self.manual_threshold.get()
        
        # Filter results by threshold
        filtered_results = [(path, score) for path, score in self.cached_results 
                           if score >= self.min_score]
        
        # Update status with threshold info
        result_count = len(filtered_results)
        total_count = len(self.cached_results)
        threshold_pct = self.min_score * 100
        
        if result_count == 0:
            self.status_text.set(f"No results above {threshold_pct:.1f}% similarity")
        elif result_count < total_count:
            percentage = (result_count / total_count) * 100
            query = self.query_text.get().strip() or os.path.basename(self.query_image.get())
            self.status_text.set(
                f"Found {result_count} matches for '{query}' with {threshold_pct:.1f}%+ similarity ({percentage:.1f}% of dataset)")
        else:
            self.status_text.set(f"All {result_count} results above {threshold_pct:.1f}% similarity")
        
        # Store all filtered results
        self.result_paths = filtered_results
        
        # Reset to first page
        self.current_page = 0
        self._update_results_page()

    def _image_search(self):
        if not os.path.isfile(self.query_image.get()):
            messagebox.showerror("Error", "Please select a valid image file")
            return

        if not self.model or not self.processor:
            messagebox.showinfo("Info", "Please load a model first")
            return

        if not self.embeddings:
            messagebox.showinfo("Info", "Please load embeddings first")
            return

        def search_task():
            try:
                query_path = self.query_image.get()
                self.status_text.set(f"Searching for similar images to {os.path.basename(query_path)}...")
                self.progress_var.set(10)

                # Load and process query image
                query_image = Image.open(query_path).convert('RGB')
                inputs = self.processor(images=query_image, return_tensors="pt")

                # Move to GPU if available
                if torch.cuda.is_available():
                    inputs = {k: v.cuda() for k, v in inputs.items() if k != 'text'}

                with torch.no_grad():
                    image_features = self.model.get_image_features(**inputs)
                    query_embedding = image_features.cpu().numpy()[0]
                    # Normalize
                    query_embedding = query_embedding / np.linalg.norm(query_embedding)

                self.progress_var.set(50)

                # Search for similar images
                similarities = []
                for path, data in self.embeddings.items():
                    if 'embedding' not in data:
                        continue

                    image_embedding = np.array(data['embedding'])
                    # In case the stored embedding is not normalized
                    image_embedding = image_embedding / np.linalg.norm(image_embedding)

                    similarity = np.dot(query_embedding, image_embedding)
                    similarities.append((path, similarity))

                # Sort by similarity (highest first)
                similarities.sort(key=lambda x: x[1], reverse=True)

                self.progress_var.set(90)

                # Get top results
                top_n = self.num_results.get()
                self.result_paths = [(path, score) for path, score in similarities[:top_n]]

                # Store all results in cache
                self.cached_results = similarities
                
                # Update display with threshold
                self._update_displayed_results()
                
                self.progress_var.set(100)
            except Exception as e:
                self.status_text.set(f"Error during search: {str(e)}")
                messagebox.showerror("Error", f"Search failed: {str(e)}")

        threading.Thread(target=search_task, daemon=True).start()

    def _display_results(self):
        # Clear current results
        for widget in self.results_container.winfo_children():
            widget.destroy()

        self.thumbnails = []  # Clear thumbnail references

        # Reset current page
        self.current_page = 0
        self._update_results_page()

    def _update_results_page(self):
        # Cancel any pending thumbnail loads
        self.thumbnail_load_queue = []
        self.is_loading_thumbnails = False
        
        # Clear current page
        for widget in self.results_container.winfo_children():
            widget.destroy()

        start_idx = self.current_page * self.results_per_page
        end_idx = min(start_idx + self.results_per_page, len(self.result_paths))

        # Update page label
        total_pages = max(1, (len(self.result_paths) - 1) // self.results_per_page + 1)
        self.page_label.config(text=f"Page {self.current_page + 1} of {total_pages}")

        # Calculate layout
        container_width = self.canvas.winfo_width()
        if container_width <= 1:  # Canvas not yet properly sized
            self.root.update_idletasks()
            container_width = self.canvas.winfo_width()
        
        scrollbar_width = 20
        available_width = max(container_width - scrollbar_width, 200)  # Minimum width
        thumbnail_width = 200
        padding = 10
        min_spacing = 5
        
        columns = max(1, (available_width + min_spacing) // (thumbnail_width + padding + min_spacing))
        remaining_width = available_width - (columns * (thumbnail_width + padding))
        extra_spacing = remaining_width // (columns + 1) if columns > 1 else 0

        # Reset grid configuration
        for i in range(columns):
            self.results_container.grid_columnconfigure(i, weight=1, minsize=thumbnail_width)

        # Create all frames and placeholders first
        frames = []
        for i, (path, score) in enumerate(self.result_paths[start_idx:end_idx]):
            row = i // columns
            col = i % columns

            # Create frame
            img_frame = ttk.Frame(self.results_container)
            img_frame.grid(row=row, column=col, padx=(extra_spacing + 5), pady=5, sticky=tk.NW)
            frames.append((img_frame, path, score))

            # Create placeholder
            placeholder = ttk.Label(img_frame, text="Loading...", width=20, anchor="center")
            placeholder.pack(pady=80)

        # Start loading thumbnails in batches
        self.is_loading_thumbnails = True
        self._load_thumbnail_batch(frames)

    def _load_thumbnail_batch(self, frames, batch_size=5):
        """Load thumbnails in small batches to prevent UI freezing"""
        if not frames or not self.is_loading_thumbnails:
            self.is_loading_thumbnails = False
            return
        
        # Process a batch
        batch = frames[:batch_size]
        remaining = frames[batch_size:]
        
        def process_batch():
            for img_frame, path, score in batch:
                try:
                    if not img_frame.winfo_exists():
                        continue
                    
                    # Clear placeholder
                    for widget in img_frame.winfo_children():
                        widget.destroy()

                    # Load thumbnail
                    thumb_path = self._get_thumbnail_path(path)
                    if path in self.thumbnail_cache:
                        photo = self.thumbnail_cache[path]
                    else:
                        try:
                            if os.path.exists(thumb_path):
                                # Fast load for existing thumbnails
                                with Image.open(thumb_path) as img:
                                    photo = ImageTk.PhotoImage(img)
                            else:
                                # Optimized thumbnail generation
                                with Image.open(path) as img:
                                    # Convert to RGB only if needed
                                    if img.mode not in ('RGB', 'L'):
                                        img = img.convert('RGB')
                                    
                                    # Calculate target size maintaining aspect ratio
                                    target_size = (200, 200)
                                    img.thumbnail(target_size, Image.Resampling.LANCZOS)
                                    
                                    # Use optimize flag and higher compression
                                    img.save(thumb_path, 'WEBP', 
                                           quality=80,  # Slightly lower quality
                                           method=4,    # Faster compression
                                           optimize=True)
                                    photo = ImageTk.PhotoImage(img)
                        
                            self.thumbnail_cache[path] = photo
                        except Exception as e:
                            print(f"Error creating thumbnail for {path}: {e}")
                            # Create error placeholder
                            error_img = Image.new('RGB', (200, 200), color='gray')
                            photo = ImageTk.PhotoImage(error_img)

                    # Create and pack image label
                    img_label = ttk.Label(img_frame, image=photo)
                    img_label.pack()

                    # Add filename and score labels
                    name_label = ttk.Label(img_frame, text=os.path.basename(path), wraplength=180)
                    name_label.pack()
                    # Display score as percentage
                    score_pct = score * 100
                    score_label = ttk.Label(img_frame, text=f"Score: {score_pct:.1f}%")
                    score_label.pack()

                    # Bind events
                    img_label.bind("<Button-1>", lambda e, p=path: self._open_image(p))
                    
                    # Add context menu
                    img_context_menu = tk.Menu(img_label, tearoff=0)
                    img_context_menu.add_command(label="Open Image", 
                                               command=lambda p=path: self._open_image(p))
                    img_context_menu.add_command(label="Search Similar Images", 
                                               command=lambda p=path: self._search_by_result(p))
                    
                    def show_context_menu(event, menu=img_context_menu):
                        menu.tk_popup(event.x_root, event.y_root)
                        
                    img_label.bind("<Button-3>", show_context_menu)
                    if sys.platform == 'darwin':
                        img_label.bind("<Button-2>", show_context_menu)

                except Exception as e:
                    print(f"Error loading thumbnail for {path}: {e}")

            # Schedule next batch
            if remaining:
                self.root.after(50, lambda: self._load_thumbnail_batch(remaining))

        # Run batch processing in a thread
        threading.Thread(target=process_batch, daemon=True).start()

    def _get_thumbnail_path(self, image_path):
        """Get path for cached thumbnail"""
        # Create hash of original path to use as filename
        path_hash = hashlib.md5(image_path.encode()).hexdigest()
        return os.path.join(self.thumbnail_dir, f"{path_hash}.webp")

    def _open_image(self, image_path):
        """Open the image in the default image viewer"""
        if sys.platform == 'win32':
            os.startfile(image_path)
        elif sys.platform == 'darwin':  # macOS
            os.system(f'open "{image_path}"')
        else:  # Linux
            os.system(f'xdg-open "{image_path}"')

    def _show_about(self):
        about_text = """CLIP Image Search

A simple GUI application for searching images using CLIP embeddings.
Supports both text-based and image-based queries.

- Use text search to find images that match a description
- Use image search to find visually similar images
- Click on results to open images
"""
        messagebox.showinfo("About CLIP Image Search", about_text)

    def _save_config(self):
        """Save current configuration"""
        config = {
            "embeddings_file": self.embeddings_file.get(),
            "model_name": self.model_name.get()
        }

        try:
            config_dir = os.path.join(os.path.expanduser("~"), ".clip_search")
            os.makedirs(config_dir, exist_ok=True)

            with open(os.path.join(config_dir, "config.json"), 'w') as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Error saving config: {e}")

    def _load_config(self):
        """Load saved configuration"""
        try:
            config_path = os.path.join(os.path.expanduser("~"), ".clip_search", "config.json")

            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)

                if "embeddings_file" in config:
                    self.embeddings_file.set(config["embeddings_file"])
                if "model_name" in config:
                    self.model_name.set(config["model_name"])
        except Exception as e:
            print(f"Error loading config: {e}")

    def _browse_gen_directory(self):
        """Browse for input directory and set default output path."""
        directory = filedialog.askdirectory(title="Select Images Directory")
        if directory:
            self.gen_directory.set(directory)
            
            # If no output file is set, default to either:
            # 1. Currently loaded embeddings file
            # 2. Directory name + _embeddings.json.zst
            if not self.gen_output.get():
                if self.embeddings_file.get():
                    self.gen_output.set(self.embeddings_file.get())
                else:
                    dir_name = os.path.basename(directory)
                    self.gen_output.set(os.path.join(directory, f"{dir_name}_embeddings.json.zst"))

    def _browse_gen_output(self):
        """Browse for output file, defaulting to the currently loaded embeddings file."""
        initial_file = self.embeddings_file.get() or ""
        
        filename = filedialog.asksaveasfilename(
            title="Save Embeddings As",
            initialfile=os.path.basename(initial_file),
            initialdir=os.path.dirname(initial_file) if initial_file else None,
            filetypes=(
                ("Compressed JSON", "*.json.zst"),
                ("JSON files", "*.json"),
                ("All files", "*.*")
            ),
            defaultextension=".json.zst"
        )
        if filename:
            self.gen_output.set(filename)

    def _log(self, message):
        """Add message to log with timestamp"""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def _generate_embeddings(self):
        # Validate input
        if not self.gen_directory.get():
            messagebox.showerror("Error", "Please select an images directory")
            return
        
        if not self.gen_output.get():
            messagebox.showerror("Error", "Please specify an output file")
            return
        
        if not self.gen_model.get():
            messagebox.showerror("Error", "Please specify a model")
            return
        
        # Clear log
        self.log_text.delete(1.0, tk.END)
        
        def generation_task():
            try:
                # Update status
                self.gen_status.set("Starting embedding generation...")
                self.gen_progress.set(0)
                
                # Load existing embeddings if any
                output_path = self.gen_output.get()
                self._log(f"Loading existing embeddings from {output_path}")
                existing_embeddings = utils.load_embeddings(output_path)
                self._log(f"Loaded {len(existing_embeddings)} existing embeddings")
                
                # Get image files
                self._log(f"Scanning directory {self.gen_directory.get()} for images...")
                image_files, skipped_files = utils.get_image_files(self.gen_directory.get())
                
                # Log skipped files
                if skipped_files:
                    self._log(f"\nSkipped {len(skipped_files)} invalid or non-image files:")
                    for path, reason in skipped_files:
                        self._log(f"  - {os.path.basename(path)}: {reason}")
                    self._log("")  # Add blank line for readability
                
                self._log(f"Found {len(image_files)} valid images")
                
                # Find images that need processing
                self._log("Identifying new images to process...")
                to_process = utils.find_images_to_process(image_files, existing_embeddings)
                self._log(f"Found {len(to_process)} images to process")
                
                if not to_process:
                    self._log("No new images to process!")
                    self.gen_status.set("Complete - no new images")
                    self.gen_progress.set(100)
                    return
                    
                # Load model
                model_name = self.gen_model.get()
                self._log(f"Loading model {model_name}...")
                model, processor, device, is_clip = utils.load_model(
                    model_name, 
                    use_fp16=self.gen_fp16.get(),
                    force_cpu=not torch.cuda.is_available()
                )
                
                model_type = "CLIP" if is_clip else "ViT"
                device_type = "GPU" if device == "cuda" else "CPU"
                precision = "FP16" if self.gen_fp16.get() and device == "cuda" else "FP32"
                
                self._log(f"Loaded {model_type} model on {device_type} using {precision} precision")
                
                # Process in batches
                batch_size = self.gen_batch_size.get()
                self._log(f"Using batch size of {batch_size}")
                
                new_count = 0
                total_batches = (len(to_process) + batch_size - 1) // batch_size
                
                for i in range(0, len(to_process), batch_size):
                    if self.gen_stop_flag:
                        self._log("Generation stopped by user")
                        self.gen_status.set("Stopped")
                        break
                        
                    batch_paths = to_process[i:i + batch_size]
                    current_batch = i // batch_size + 1
                    
                    self._log(f"Processing batch {current_batch}/{total_batches} ({len(batch_paths)} images)")
                    
                    # Process batch
                    batch_results, failed_images = generate.process_batch(batch_paths, model, processor, device, is_clip)
                    
                    # Log any failures
                    if failed_images:
                        self._log(f"\nFailed to process {len(failed_images)} images in this batch:")
                        for path, error in failed_images:
                            self._log(f"  - {os.path.basename(path)}: {error}")
                        self._log("")  # Add blank line for readability
                    
                    # Update embeddings
                    existing_embeddings.update(batch_results)
                    new_count += len(batch_results)
                    
                    # Update progress
                    progress_pct = min(100, int((i + len(batch_paths)) / len(to_process) * 100))
                    self.gen_progress.set(progress_pct)
                    self.gen_status.set(f"Processed {i + len(batch_paths)}/{len(to_process)} images")
                    
                    # Save periodically
                    if current_batch % 5 == 0 or i + batch_size >= len(to_process):
                        self._log(f"Saving {len(existing_embeddings)} embeddings...")
                        utils.save_embeddings(existing_embeddings, output_path)
                
                # Final save
                if not self.gen_stop_flag:
                    utils.save_embeddings(existing_embeddings, output_path)
                    self.gen_progress.set(100)
                    self.gen_status.set(f"Complete - {new_count} new embeddings generated")
                    self._log(f"Completed processing {len(to_process)} images")
                    self._log(f"New/updated embeddings: {new_count}")
                    self._log(f"Total embeddings in file: {len(existing_embeddings)}")
                    self._log(f"Embeddings saved to: {output_path}")
                    
                    # Offer to load the embeddings for search
                    if messagebox.askyesno("Generation Complete", 
                                        f"Generated {new_count} new embeddings. Load them for search?"):
                        self.embeddings_file.set(output_path)
                        self.notebook.select(0)  # Switch to search tab
                        self._load_embeddings()
                
            except Exception as e:
                self._log(f"Error during embedding generation: {str(e)}")
                self.gen_status.set(f"Error: {str(e)}")
                import traceback
                self._log(traceback.format_exc())
        
        # Start generation thread
        self.gen_stop_flag = False
        threading.Thread(target=generation_task, daemon=True).start()

    def _search_by_result(self, image_path):
        """Use a result image as a new search query"""
        self.query_image.set(image_path)
        self.query_text.set("")  # Clear any text query
        self._image_search()

    def _on_window_resize(self, event):
        # Only respond to root window resizing, not child widgets
        if event.widget == self.root:
            # Ignore resize events while loading thumbnails
            if self.is_loading_thumbnails:
                return
            
            # Ignore small resize changes
            if hasattr(self, '_last_width'):
                width_change = abs(event.width - self._last_width)
                if width_change < 50:  # Ignore small width changes
                    return
                
            self._last_width = event.width
            
            # Cancel any pending resize timer
            if hasattr(self, "_resize_timer"):
                self.root.after_cancel(self._resize_timer)
            
            # Schedule a single resize update
            self._resize_timer = self.root.after(250, self._delayed_resize_update)

    def _delayed_resize_update(self):
        """Handle the actual resize update after delay"""
        if self.result_paths:
            self._update_results_page()

    def _clear_results(self):
        """Clear all search results and caches"""
        self.result_paths = []
        self.cached_results = []
        self.thumbnail_cache.clear()  # Clear thumbnail cache
        self.thumbnail_load_queue = []
        self.current_page = 0

        # Clear display
        for widget in self.results_container.winfo_children():
            widget.destroy()

        self.page_label.config(text="Page 1")
        self.status_text.set("Results cleared")

        # Optionally clear disk cache if it's getting too large
        cache_size = sum(os.path.getsize(os.path.join(self.thumbnail_dir, f)) 
                        for f in os.listdir(self.thumbnail_dir))
        max_cache_size = 500 * 1024 * 1024  # 500MB
        
        if cache_size > max_cache_size:
            try:
                for f in os.listdir(self.thumbnail_dir):
                    os.remove(os.path.join(self.thumbnail_dir, f))
            except Exception as e:
                print(f"Error clearing thumbnail cache: {e}")

        # Clean up thumbnail cache
        self._cleanup_thumbnail_cache()

    def _prev_page(self):
        """Go to previous page of results"""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_results_page()

    def _next_page(self):
        """Go to next page of results or load more if available"""
        max_page = (len(self.result_paths) - 1) // self.results_per_page
        if self.current_page < max_page:
            self.current_page += 1
            self._update_results_page()
        else:
            # We're on the last page. Try to load more results from cache
            current_count = self.num_results.get()
            new_count = current_count + 20
            
            # Check if we have more results in cache
            if new_count <= len(self.cached_results):
                self.num_results.set(new_count)
                self.result_paths = self.cached_results[:new_count]
                self.current_page = max_page
                self._update_results_page()
            else:
                # Only perform new search if we need more results than cached
                self.num_results.set(new_count)
                if self.query_text.get().strip():
                    self._text_search()
                elif self.query_image.get():
                    self._image_search()

    def _on_frame_configure(self, event=None):
        """Reset the scroll region to encompass the inner frame"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        """When canvas is resized, resize the inner frame to match"""
        # Update the width of the results container to match canvas
        canvas_width = event.width
        self.canvas.itemconfig(self.canvas_window, width=canvas_width)

    def _cleanup_thumbnail_cache(self):
        """Clean up old thumbnails to prevent cache from growing too large"""
        try:
            # Get all thumbnail files and their modification times
            thumb_files = []
            for f in os.listdir(self.thumbnail_dir):
                path = os.path.join(self.thumbnail_dir, f)
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                thumb_files.append((path, mtime, size))
            
            # Sort by modification time (oldest first)
            thumb_files.sort(key=lambda x: x[1])
            
            # Calculate total size
            total_size = sum(size for _, _, size in thumb_files)
            max_cache_size = 500 * 1024 * 1024  # 500MB
            
            # Remove old files until we're under the limit
            while total_size > max_cache_size and thumb_files:
                path, _, size = thumb_files.pop(0)  # Remove oldest
                try:
                    os.remove(path)
                    total_size -= size
                except OSError:
                    pass
                
        except Exception as e:
            print(f"Error cleaning thumbnail cache: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = CLIPSearchApp(root)
    root.mainloop()