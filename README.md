# LivePhotoUploader

LivePhotoUploader is a web application that allows users to upload photos from their iPhone via a website. The application supports the latest Apple photo formats, including images with live views (e.g., HEIC and Live Photos), and processes them to generate JPEG exports in various sizes. Uploaded photos are organized into user-defined categories and stored on the server with a structured directory system.

## Artifitial Intelligence query:

**Objective:** Develop a web application that enables users to upload photos from their iPhone, including the latest Apple photo formats, through a web interface. The application stores these photos on a server and generates JPEG variants in different resolutions. Users can manage categories to organize their photos.

### Features

- **Photo Uploading:**
  - Users can upload photos directly from their iPhone via the web interface.
  - Support for the latest Apple photo formats with live views (not limited to JPEG or PNG).

- **Photo Processing:**
  - After uploading, photos are stored in a designated directory on the server.
  - The application generates JPEG exports in various sizes:
    - **Largest resolution**
    - **Medium resolution**
    - **Thumbnail size** (small size suitable for previews)

- **Category Management:**
  - Users can create and delete categories.
  - Each category corresponds to a directory within the upload directory.
  - Within each category, there are four subdirectories:
    - **source/** (original uploaded files)
    - **largest/** (largest resolution JPEGs)
    - **medium/** (medium resolution JPEGs)
    - **thumbnail/** (thumbnail JPEGs)

- **User Interface:**
  - The web application includes a front-end with user-friendly design.
  - Incorporates appropriate coloring and CSS styling (not just black and white).
  - Allows easy navigation and interaction for uploading and managing photos and categories.

### Directory Structure

The uploaded photos and generated JPEGs are organized in the following directory structure:

```
uploads/
  category_name/
    source/
    largest/
    medium/
    thumbnail/
```

- **uploads/**: Root directory for all uploaded content.
- **category_name/**: User-defined categories for organizing photos.
- **source/**: Contains the original uploaded photos.
- **largest/**: Contains the largest resolution JPEG versions.
- **medium/**: Contains medium resolution JPEG versions.
- **thumbnail/**: Contains thumbnail-sized JPEG versions.

### Technical Specifications

- **Hosting Environment:**
  - The application will run on Kubernetes.
  - Includes a Dockerfile and source code in the project repository.
  - Kubernetes deployment steps are not included (assumed to be handled separately).

- **Storage:**
  - Uses local directories for storage.
  - No need for S3 or external storage solutions.
  - An S3FS plugin will be used if necessary to interface with S3-like storage.

- **Technology Stack:**
  - Programming language and frameworks are flexible and can be chosen as needed.
  - The application does not need to be separated into front-end and back-end; a monolithic structure is acceptable.
  - Must include support for processing the latest Apple photo formats (e.g., HEIC, Live Photos).

- **Design:**
  - The web interface should have a pleasant design with appropriate coloring and CSS styling.
  - Should not be a plain black-and-white interface.

## Instructions for Generating the Application

This description is intended to be used when requesting ChatGPT to generate the source code and the entire application.

---

**Note:** The application should be developed with the capability to read and process the latest Apple photo formats that include live views, which are not standard JPEG or PNG files. The storage system relies on a local directory structure, and while it doesn't require AWS S3, the use of an S3FS plugin is anticipated for storage purposes.
