# Test Writer Memory - mesh-generator

## Testing Framework

- **Framework**: pytest (version 8.0+)
- **Runner**: `poetry run pytest -v`
- **Location**: `/tests/` directory
- **Pattern**: Test files use `test_*.py` naming convention

## Project Setup

- **Dependency Manager**: Poetry
- **Python Version**: 3.10+ (running on 3.14.0)
- **Key Dependencies**: numpy, rasterio, requests, shapely, flask, pyyaml
- **Lock File**: Must run `poetry lock` if pyproject.toml changes significantly

## Testing Patterns

### Test Structure
- Use class-based test organization (e.g., `TestBuildGrid`, `TestFetchElevations`)
- Follow AAA pattern: Arrange-Act-Assert
- Use descriptive test method names: `test_<function>_<scenario>`
- Use docstrings to explain what each test verifies

### Assertions
- Use `pytest.approx()` for floating-point comparisons
- Use `np.testing.assert_array_almost_equal()` for numpy array comparisons
- Use specific assertions with meaningful context

### Mocking External Dependencies
- **HTTP Requests**: Use `unittest.mock.patch` to mock `requests.post`
- **Pattern**: Mock at module level (e.g., `"generator.elevation.requests.post"`)
- **API Response Structure**: Return MagicMock with `.json()` and `.raise_for_status()` methods
- Always mock external services to prevent real network calls in tests

### Temporary Files
- Use `tempfile.TemporaryDirectory()` for test artifacts
- Validate file existence with `Path(output_path).exists()`
- Clean up handled automatically by context manager

## Gotchas and Lessons Learned

### Floating Point Precision
- **Issue**: Grid calculations with `ceil()` can produce unexpected results due to floating point precision
- **Example**: `0.1 / 0.05 = 2.0000000000000284`, so `ceil()` returns 3, not 2
- **Solution**: Test with actual calculated values rather than manual math
- **When Testing Grid Functions**: Run calculation separately to verify expected dimensions

### GeoTIFF Validation
- **Reading Back**: Use `rasterio.open()` to validate written GeoTIFF files
- **Key Checks**:
  - Count (number of bands)
  - Height/width (dimensions)
  - CRS (coordinate reference system)
  - Data type (e.g., float32)
  - Bounds (geographic extent)
  - Compression setting
- **Data Validation**: Read band with `src.read(1)` and compare arrays

### Batching Logic
- **Batch Size**: Open-Elevation API has BATCH_SIZE = 200
- **Test with Multiple Batches**: Use 15x15 grid (225 points) to test batch handling
- **Mock Side Effects**: Use `side_effect=[response1, response2]` for sequential responses

## Test Coverage for elevation.py

- **_build_grid**: 5 tests covering dimensions, bounds, edge cases, minimum size
- **_fetch_elevations**: 4 tests covering single batch, multiple batches, errors, request format
- **_write_geotiff**: 4 tests covering basic write, square grid, large grid, compression
- **fetch_and_write_elevation**: 3 tests covering integration, defaults, component calls

## Commands

```bash
# Install dependencies
poetry install

# Update lock file
poetry lock

# Run specific test file
poetry run pytest tests/test_elevation.py -v

# Run all tests
poetry run pytest -v

# Run with coverage
poetry run pytest --cov=generator --cov-report=html
```
