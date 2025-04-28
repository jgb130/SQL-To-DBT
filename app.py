import requests
import yaml
import sqlparse
import re
import os
from urllib.parse import urlparse
from sqlparse import tokens as T
# Import jsonify for returning JSON responses, session for storing data
from flask import Flask, render_template, request, flash, jsonify, session

# --- Flask App Setup ---
app = Flask(__name__)
# Required for session management and flashing messages
app.secret_key = os.urandom(24) # Replace with a strong, persistent secret key in production

# --- Constants ---
MODELS_DIR = 'models'
SCHEMA_FILE_NAMES = ['schema.yml', 'sources.yml']

# --- Helper Functions (Largely unchanged, but return values adapted) ---

def get_github_api_url(github_repo_url):
    """Constructs the GitHub API URL for repository contents."""
    # Basic validation
    if not github_repo_url or not github_repo_url.startswith("https://github.com/"):
         return None
    parsed_url = urlparse(github_repo_url)
    path_parts = parsed_url.path.strip('/').split('/')
    if len(path_parts) >= 2:
        owner, repo = path_parts[0], path_parts[1]
        if repo.endswith('.git'):
            repo = repo[:-4]
        return f"https://api.github.com/repos/{owner}/{repo}/contents/"
    else:
        return None

def fetch_github_repo_files(repo_api_url, path=""):
    """
    Recursively fetches file paths and content relevant for dbt models/sources.
    Returns a dictionary {file_path: file_content_string} or None on major error.
    """
    # (Keep the implementation from the previous version, ensuring it returns None on critical errors)
    # ... (fetch_github_repo_files implementation from previous version) ...
    files_content = {}
    headers = {} # Add {'Authorization': f'token YOUR_GITHUB_TOKEN'} for private repos/rate limits

    current_api_url = repo_api_url + path
    print(f"Fetching contents from: {current_api_url}") # Keep logging for debug

    try:
        list_headers = headers.copy()
        list_headers['Accept'] = 'application/vnd.github.v3+json'
        # Increased timeout slightly for potentially larger repos
        response = requests.get(current_api_url, headers=list_headers, timeout=20)
        response.raise_for_status()
        items = response.json()

        if isinstance(items, dict) and items.get('type') == 'file':
             items = [items]
        elif not isinstance(items, list):
             print(f"Warning: Expected list from API for path '{path}', got {type(items)}. Skipping.")
             return files_content # Return empty dict for this path, not necessarily fatal

        for item in items:
            if not isinstance(item, dict): continue
            item_path = item.get('path')
            item_type = item.get('type')
            if not item_path or not item_type: continue

            if item_type == 'file':
                is_model_sql = item_path.startswith(MODELS_DIR + '/') and item_path.endswith('.sql')
                is_schema_yml = item_path.endswith('.yml')

                if is_model_sql or is_schema_yml:
                    print(f"  Fetching relevant file: {item_path}")
                    try:
                        content_url = item.get('download_url')
                        fetch_headers = headers.copy()
                        if not content_url:
                             content_url = item.get('url')
                             if not content_url: continue
                             print(f"  Attempting fetch via API URL: {content_url}")
                             fetch_headers['Accept'] = 'application/vnd.github.v3.raw'
                        else:
                             print(f"  Fetching via download_url: {content_url}")

                        # Increased timeout for file download
                        file_response = requests.get(content_url, headers=fetch_headers, timeout=15)
                        file_response.raise_for_status()
                        files_content[item_path] = file_response.content.decode('utf-8', errors='ignore')
                    except requests.exceptions.RequestException as e:
                        print(f"Warning: Could not fetch file content for {item_path}: {e}")
                    except Exception as e:
                         print(f"Warning: Error processing file content for {item_path}: {e}")

            elif item_type == 'dir':
                if item_path == '.git': continue
                print(f"  Entering directory: {item_path}")
                subdir_content = fetch_github_repo_files(repo_api_url, item_path)
                if subdir_content: # Check if recursive call returned content
                    files_content.update(subdir_content)

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error fetching {current_api_url}: {e.response.status_code}")
        return None # Indicate critical fetch failure for this path/repo
    except requests.exceptions.RequestException as e:
        print(f"Network Error fetching {current_api_url}: {e}")
        return None # Indicate critical fetch failure
    except Exception as e:
        print(f"Unexpected error processing API response for {current_api_url}: {e}")
        return None # Indicate critical failure

    return files_content


def build_reference_map(files_content):
    """
    Builds the dbt reference map. Returns the map.
    Map format: {raw_name: (type, arg1, arg2, file_path)}
    """
    # (Keep the implementation from the previous version)
    # ... (build_reference_map implementation from previous version) ...
    if not files_content:
         return {} # Return empty map if no files

    reference_map = {}
    yaml_contents = []

    # Process Models (.sql files)
    for file_path, content in files_content.items():
        if file_path.startswith(MODELS_DIR + '/') and file_path.endswith('.sql'):
            model_name = os.path.splitext(os.path.basename(file_path))[0]
            if model_name in reference_map:
                print(f"Warning: Duplicate model definition for '{model_name}'. Overwriting.")
            reference_map[model_name] = ('model', model_name, None, file_path)
            print(f"  Registered Model: {model_name} (from {file_path})")
        elif file_path.endswith('.yml'):
             yaml_contents.append((file_path, content))

    # Process Sources (.yml files)
    for file_path, content in yaml_contents:
        print(f"Processing YAML file: {file_path}")
        try:
            data = yaml.safe_load(content)
            if not isinstance(data, dict): continue

            sources = data.get('sources')
            if isinstance(sources, list):
                for source_def in sources:
                     if not isinstance(source_def, dict): continue
                     source_name = source_def.get('name')
                     tables = source_def.get('tables')
                     if not source_name or not isinstance(tables, list): continue

                     for table_def in tables:
                         if not isinstance(table_def, dict): continue
                         table_name = table_def.get('name')
                         if table_name and isinstance(table_name, str):
                             if table_name not in reference_map:
                                 reference_map[table_name] = ('source', source_name, table_name, file_path)
                                 print(f"  Registered Source: {source_name}.{table_name} (key: {table_name}, from {file_path})")
                             else:
                                 if reference_map[table_name][0] == 'model':
                                      print(f"  Info: Source '{source_name}.{table_name}' conflicts with model '{table_name}'. Prioritizing model.")
        except yaml.YAMLError as e:
            print(f"Warning: Error parsing YAML {file_path}: {e}")
        except Exception as e:
            print(f"Warning: Unexpected error processing YAML {file_path}: {e}")

    return reference_map


def extract_tables_from_sql(sql):
    """
    Extracts potential table identifiers from FROM/JOIN clauses. Returns a set.
    Handles simple CTEs and aliases.
    """
    # (Keep the implementation from the previous version)
    # ... (extract_tables_from_sql implementation from previous version) ...
    tables = set()
    parsed = sqlparse.parse(sql)
    cte_names = set()

    # Identify CTEs
    for stmt in parsed:
        tokens = list(stmt.flatten())
        in_with = False
        name_next = False
        paren_level = 0 # Track parenthesis level for CTE definition end
        for i, token in enumerate(tokens):
            if token.is_keyword and token.normalized == 'WITH':
                in_with, name_next = True, True
                continue

            # Basic check to exit WITH clause processing
            if in_with and paren_level == 0 and token.is_keyword and token.normalized in ('SELECT', 'INSERT', 'UPDATE', 'DELETE') and isinstance(token.parent, sqlparse.sql.Statement):
                 in_with, name_next = False, False
                 break # End of WITH clause definition section

            if in_with and name_next and token.ttype is T.Name:
                cte_names.add(token.value)
                name_next = False
            # Track parenthesis to better detect end of CTE definition
            if in_with and token.ttype is T.Punctuation:
                if token.value == '(':
                    paren_level += 1
                elif token.value == ')':
                    paren_level = max(0, paren_level - 1) # Avoid going below 0
                # If parenthesis level is back to 0 and we see a comma, expect next CTE name
                elif token.value == ',' and paren_level == 0:
                    name_next = True

    print(f"  Found CTE names (ignored): {cte_names}")

    # Identify tables
    for stmt in parsed:
        from_seen, join_seen, skip_next = False, False, False
        tokens = [t for t in stmt.flatten() if not t.is_whitespace]
        for i, token in enumerate(tokens):
            val, typ = token.value, token.ttype
            # Use token.normalized safely
            norm = token.normalized if hasattr(token, 'normalized') and token.normalized else None

            # Reset logic
            if token.is_keyword and norm in ('WHERE', 'GROUP', 'ORDER', 'LIMIT', 'ON', 'USING', 'UNION', 'INTERSECT', 'EXCEPT', 'WINDOW', 'PARTITION', 'FETCH', 'OFFSET'):
                from_seen, join_seen, skip_next = False, False, False
                continue
            if typ is T.Punctuation and val != '.':
                prev_token = tokens[i-1] if i > 0 else None
                # Don't reset if comma follows a name (multi-table FROM/JOIN)
                if not ((from_seen or join_seen) and prev_token and prev_token.ttype is T.Name and val == ','):
                    from_seen, join_seen = False, False
                skip_next = False
                continue

            # Set logic
            if token.is_keyword and norm == 'FROM':
                from_seen, join_seen, skip_next = True, False, False
                continue
            if token.is_keyword and norm == 'JOIN':
                join_seen, from_seen, skip_next = True, False, False
                continue

            # Identification logic
            is_potential_table = typ is T.Name or isinstance(token, sqlparse.sql.Identifier)
            if (from_seen or join_seen) and is_potential_table:
                if skip_next:
                    skip_next = False
                    continue

                # Use get_name() as a fallback if get_real_name isn't there or fails
                table_candidate = token.value
                if hasattr(token, 'get_real_name'):
                    real_name = token.get_real_name()
                    if real_name: table_candidate = real_name
                elif hasattr(token, 'get_name'):
                     name = token.get_name()
                     if name: table_candidate = name

                table_candidate = table_candidate.strip('"`\'')

                if not table_candidate: # Skip if empty after stripping quotes
                     continue

                if table_candidate in cte_names:
                    print(f"  Ignoring CTE reference: {table_candidate}")
                else:
                    table_name = table_candidate
                    # Qualified name check
                    if i + 2 < len(tokens) and tokens[i+1].value == '.' and (tokens[i+2].ttype is T.Name or isinstance(tokens[i+2], sqlparse.sql.Identifier)):
                        part2_token = tokens[i+2]
                        part2_name = part2_token.value # Default to value
                        if hasattr(part2_token, 'get_real_name'):
                            real_name_p2 = part2_token.get_real_name()
                            if real_name_p2: part2_name = real_name_p2
                        elif hasattr(part2_token, 'get_name'):
                             name_p2 = part2_token.get_name()
                             if name_p2: part2_name = name_p2
                        table_name = part2_name.strip('"`\'')
                        print(f"  Found qualified: {table_candidate}.{table_name} -> using '{table_name}'")
                    else:
                        print(f"  Found potential table: {table_name}")

                    if table_name: # Ensure not empty after potential qualification logic
                        tables.add(table_name)

                # Alias check (simplified)
                next_idx = i + (3 if (i + 2 < len(tokens) and tokens[i+1].value == '.') else 1)
                if next_idx < len(tokens):
                    next_t = tokens[next_idx]
                    # If next token is 'AS' keyword, or just a Name/Identifier (not keyword)
                    is_alias_keyword = next_t.is_keyword and next_t.normalized == 'AS'
                    is_alias_name = (next_t.ttype is T.Name or isinstance(next_t, sqlparse.sql.Identifier)) and not next_t.is_keyword

                    if is_alias_keyword:
                         # Check token *after* AS
                         if next_idx + 1 < len(tokens):
                              after_as_token = tokens[next_idx+1]
                              if (after_as_token.ttype is T.Name or isinstance(after_as_token, sqlparse.sql.Identifier)):
                                   skip_next = True # Skip the alias name itself
                    elif is_alias_name:
                         skip_next = True # Skip the alias name

    return tables


def perform_translation(reference_map, sql_query):
    """
    Performs the translation using the provided map and query.
    Returns (translated_sql_string, error_message_string_or_None).
    """
    print("--- Performing Translation ---")
    if not reference_map:
        return sql_query, "Reference map is not available or empty. Please load a model first."

    try:
        found_tables = extract_tables_from_sql(sql_query)
    except Exception as e:
         print(f"Error parsing SQL during translation: {e}")
         return sql_query, f"Error parsing the input SQL query: {e}"

    if not found_tables:
        print("No table references found in SQL to translate.")
        return sql_query, None # No error, just nothing to do

    print(f"Tables found for translation attempt: {found_tables}")

    jinja_sql_query = sql_query
    unmatched_tables = set()
    sorted_tables = sorted(list(found_tables), key=len, reverse=True)

    for table_name in sorted_tables:
        # Normalize here again just in case parser included quotes inconsistently
        normalized_table_name = table_name.strip('"`\'')
        if not normalized_table_name: continue # Skip empty names

        if normalized_table_name in reference_map:
            ref_info = reference_map[normalized_table_name]
            ref_type, arg1, arg2 = ref_info[0], ref_info[1], ref_info[2]
            jinja_call = ""
            if ref_type == 'model':
                jinja_call = f"{{{{ ref('{arg1}') }}}}"
            elif ref_type == 'source':
                jinja_call = f"{{{{ source('{arg1}', '{arg2}') }}}}"
            else: continue

            print(f"  Replacing '{normalized_table_name}' with {jinja_call}")
            # Use the original table_name (potentially with quotes) for regex matching
            pattern = r'\b' + re.escape(table_name) + r'\b'
            new_jinja_sql_query, num_replacements = re.subn(pattern, jinja_call, jinja_sql_query, flags=re.IGNORECASE)

            if num_replacements > 0:
                 jinja_sql_query = new_jinja_sql_query
                 print(f"    Replaced {num_replacements} instance(s).")
            else:
                 print(f"    Warning: Regex pattern '{pattern}' did not replace '{table_name}'.")
        else:
            unmatched_tables.add(normalized_table_name)

    if unmatched_tables:
        error_message = f"Error: The following tables were not found in the loaded dbt project: {', '.join(sorted(list(unmatched_tables)))}"
        print(error_message)
        return sql_query, error_message # Return original SQL and error

    print("--- Translation Complete ---")
    return jinja_sql_query, None


def create_file_tree(files_content):
    """Converts flat file paths into a nested dictionary for tree view."""
    tree = {}
    for path in sorted(files_content.keys()):
        parts = path.split('/')
        node = tree
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            if is_last:
                # Ensure parent node exists
                if part not in node:
                     node[part] = {'_type': 'file'} # Mark as file
            else:
                # Ensure parent node exists and mark as folder
                if part not in node:
                    node[part] = {'_type': 'folder', 'children': {}}
                elif '_type' not in node[part]: # Ensure existing nodes are marked
                     node[part]['_type'] = 'folder'
                     if 'children' not in node[part]: node[part]['children'] = {}

                node = node[part]['children'] # Move deeper into the tree
    return tree

# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page."""
    # Clear session on new page load if desired
    # session.pop('reference_map', None)
    # session.pop('file_tree', None)
    return render_template('index_ajax.html') # Use a new template name

@app.route('/load_model', methods=['POST'])
def load_model():
    """Loads dbt model from GitHub URL via AJAX."""
    github_url = request.json.get('github_url')
    if not github_url:
        return jsonify({'success': False, 'error': 'GitHub URL is required.'}), 400

    repo_api_url = get_github_api_url(github_url)
    if not repo_api_url:
        return jsonify({'success': False, 'error': 'Invalid GitHub repository URL format.'}), 400

    print(f"Attempting to load model from: {github_url}")
    files_content = fetch_github_repo_files(repo_api_url)

    if files_content is None:
        return jsonify({'success': False, 'error': 'Failed to fetch repository files. Check URL, permissions, or network connection.'}), 500
    if not files_content:
         return jsonify({'success': False, 'error': 'No relevant dbt files (.sql in models/, .yml) found in the repository.'}), 404 # Not Found might be appropriate

    print("Building reference map...")
    reference_map = build_reference_map(files_content)
    if not reference_map: # build_reference_map now returns {} if files_content was valid but no refs found
         print("Warning: No models or sources found in the fetched files.")
         # Proceed, but map will be empty

    print("Creating file tree...")
    file_tree = create_file_tree(files_content)

    # Store map and tree in session for later use by /translate
    session['reference_map'] = reference_map
    # Storing the full tree might exceed session size limits for large repos
    # Consider storing only file paths list if tree generation is fast on client
    session['file_tree'] = file_tree # Storing for simplicity in POC

    print("Model loaded successfully.")
    return jsonify({
        'success': True,
        'message': f'Successfully loaded {len(reference_map)} models/sources.',
        'file_tree': file_tree # Send tree structure to client
        })


@app.route('/translate', methods=['POST'])
def translate_ajax():
    """Handles the translation request via AJAX using stored model data."""
    sql_query = request.json.get('sql_query')
    if not sql_query:
        return jsonify({'success': False, 'error': 'SQL query is required.'}), 400

    # Retrieve reference map from session
    reference_map = session.get('reference_map')
    if reference_map is None: # Check if None explicitly, {} is valid (empty map)
        return jsonify({'success': False, 'error': 'Model not loaded. Please load a model first.'}), 400

    translated_sql, error = perform_translation(reference_map, sql_query)

    if error:
        return jsonify({'success': False, 'error': error}) # Send error back
    else:
        return jsonify({'success': True, 'translated_sql': translated_sql}) # Send result


# --- Main Execution ---
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0') # host='0.0.0.0' makes it accessible on network if needed
