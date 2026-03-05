// Upload form interactivity
(function () {
    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");
    const fileInfo = document.getElementById("file-info");
    const fileName = document.getElementById("file-name");
    const clearBtn = document.getElementById("clear-file");
    const submitBtn = document.getElementById("submit-btn");
    const form = document.getElementById("upload-form");
    const processing = document.getElementById("processing");

    // Click to browse
    dropZone.addEventListener("click", () => fileInput.click());

    // Drag & drop
    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("dragover");
    });
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer.files.length) {
            fileInput.files = e.dataTransfer.files;
            showFile(e.dataTransfer.files[0]);
        }
    });

    // File selected via browse dialog
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length) {
            showFile(fileInput.files[0]);
        }
    });

    // Clear selection
    clearBtn.addEventListener("click", () => {
        fileInput.value = "";
        fileInfo.style.display = "none";
        submitBtn.disabled = true;
    });

    // Show file info
    function showFile(file) {
        const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
        fileName.textContent = file.name + " (" + sizeMB + " MB)";
        fileInfo.style.display = "flex";
        submitBtn.disabled = false;
    }

    // Show processing overlay on submit
    form.addEventListener("submit", () => {
        submitBtn.disabled = true;
        submitBtn.textContent = "Processing\u2026";
        processing.style.display = "flex";
    });
})();
