"""
This script provides a command-line interface (CLI) to extract metadata from a Metabase
instance and model it as a graph in a Neo4j database or a Cypher file.
It connects to a Metabase API to fetch metadata about various entities such as:
- Databases
- Schemas
- Tables
- Fields (optional)
- Collections
- Cards (Questions)
- Dashboards
The script intelligently parses native SQL queries within Metabase cards using `sqlglot`
to identify dependencies between cards and their source tables, including dependencies
on other cards.
The extracted metadata is then transformed into Cypher queries to create a graph
representation of the data lineage and relationships. For example, a Card `SOURCE`s a
Table, a Table `BELONGS_TO` a Schema, and a Dashboard `CONTAINS` a Card.
The script can be configured using environment variables for Metabase and Neo4j
connection details:
- `host`: The URL of the Metabase instance (defaults to 'http://localhost:3001').
- `neo4juri`: The Bolt URI for the Neo4j database (defaults to 'neo4j://localhost:7687').
- `user`: The username for Metabase authentication (defaults to 'a@b.com').
- `password`: The password for Metabase authentication (defaults to 'metabot1').
- `session_cookie`: An optional Metabase session cookie to bypass username/password login.
The CLI provides three main commands:
1. `neo4j`: Fetches all metadata from Metabase and writes it directly to a configured
    Neo4j database. It wipes the database before writing.
2. `cypher`: Fetches all metadata from Metabase and saves the corresponding Cypher
    queries to a file named `metadata.cypher`.
3. `database`: A limited version of `cypher` that only fetches and writes metadata
    for databases, schemas, tables, and optionally fields.
Each command has options to customize its behavior, such as including fields,
skipping archived items, and filtering by specific database IDs.
Usage:
     python main.py neo4j --fields --skip-archived --database-list "1,2"
     python main.py cypher --skip-archived
     python main.py database --fields --database-list "1"
Dependencies:
- requests: For making HTTP requests to the Metabase API.
- sqlglot: For parsing SQL queries.
- typer: For creating the command-line interface.
- neo4j: The official Python driver for Neo4j.
"""
import requests, os
from sqlglot import parse_one, exp
import typer
from neo4j import GraphDatabase
import urllib3

urllib3.disable_warnings()


app = typer.Typer()

host: str = os.environ.get('host', 'http://localhost:3001')
neo4jURI: str = os.environ.get('neo4juri', 'neo4j://localhost:7687')

login_url = f"{host}/api/session"
table_url = f"{host}/api/table"
card_url = f"{host}/api/card"
collections_url = f"{host}/api/collection"
databases_url = f"{host}/api/database"
table_url = f"{host}/api/table"
dashboard_url = f"{host}/api/dashboard"
native_card_url = f"{host}/api/dataset/native"

def _handle_request_error(e: requests.exceptions.RequestException, context: str, item_id: str = "") -> None:
    """
    Centralized error handling for API requests.
    
    Args:
        e: The request exception
        context: Description of what was being attempted
        item_id: Optional ID of the item being processed
    """
    if hasattr(e, 'response') and e.response is not None:
        if e.response.status_code == 401:
            print(f"Authentication failed: Invalid or expired session token while {context}")
            raise typer.Exit(code=1)
        else:
            print(f"HTTP error {e.response.status_code} while {context}{' for ' + item_id if item_id else ''}")
    else:
        print(f"Network error while {context}{' for ' + item_id if item_id else ''}: {e}")

def _make_api_request(session: requests.Session, url: str, context: str, item_id: str = "", method: str = "GET", **kwargs) -> dict:
    """
    Makes an API request with centralized error handling.
    
    Args:
        session: The authenticated session
        url: The API endpoint URL
        context: Description of what's being fetched
        item_id: Optional ID for error messages
        method: HTTP method (GET, POST, etc.)
        **kwargs: Additional arguments for the request
    
    Returns:
        dict: The JSON response
    """
    try:
        if method.upper() == "POST":
            response = session.post(url, verify=False, **kwargs)
        else:
            response = session.get(url, verify=False, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        _handle_request_error(e, context, item_id)
        raise typer.Exit(code=1)

def metabaseAuth() -> requests.Session:
    """
    Authenticates with a Metabase instance and returns a session object.
    
    This function establishes a connection to Metabase using either a session cookie
    or username/password credentials. It first checks for a session cookie in the
    environment variables, and if not found, falls back to username/password authentication.
    
    Returns:
        requests.Session: An authenticated session object that can be used to make
                         API requests to the Metabase instance.
    
    Environment Variables:
        user: Metabase username (defaults to 'a@b.com')
        password: Metabase password (defaults to 'metabot1')
        session_cookie: Optional Metabase session cookie for authentication
    """
    USER = os.environ.get('user', 'a@b.com')
    PASSWORD = os.environ.get('password', 'metabot1')
    login_payload = {"username": f"{USER}", "password": f"{PASSWORD}"}
    session = requests.Session()
    session_cookie = os.environ.get('session_cookie', '')
    
    if session_cookie:
        session.cookies.set('metabase.SESSION', f'{session_cookie}')
    else:
        try:
            response = session.post(login_url, json=login_payload, verify=False)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error authenticating with Metabase at {host}.")
            print(f"Please check your connection and credentials.")
            print(f"Error details: {e}")
            raise typer.Exit(code=1)
    return session

def dbAuth():
    """
    Authenticates with a Neo4j database and returns a driver instance.
    
    This function establishes a connection to a Neo4j database using the configured
    URI and verifies connectivity. It also wipes all existing data in the database
    before returning the driver instance.
    
    Returns:
        neo4j.GraphDatabase.driver: A Neo4j driver instance that can be used to
                                   execute queries against the database.
    
    Environment Variables:
        neo4juri: The Bolt URI for the Neo4j database (defaults to 'neo4j://localhost:7687')
    
    Note:
        This function will delete all existing nodes and relationships in the database
        before returning the driver.
    """
    try:
        driver = GraphDatabase.driver(neo4jURI)
        driver.verify_connectivity()
        # wipe everything before starting
        driver.execute_query('MATCH (n) DETACH DELETE n;')
        return driver
    except Exception as e:
        print(f"Error connecting to Neo4j database at {neo4jURI}")
        print(f"Please check your connection details and ensure Neo4j is running.")
        print(f"Error details: {e}")
        raise typer.Exit(code=1)

def item_generator(json_input, lookup_key):
    """
    Recursively searches for a specific key in a nested JSON structure.
    
    This function traverses a nested dictionary or list structure and yields
    all values associated with the specified lookup key. It handles both
    dictionary and list data types recursively.
    
    Args:
        json_input: The JSON data structure to search through (dict or list)
        lookup_key: The key to search for in the JSON structure
    
    Yields:
        Any: Values associated with the lookup key found in the JSON structure
    
    Example:
        For a JSON structure like {"a": {"card-id": 123}, "b": [{"card-id": 456}]}
        and lookup_key="card-id", this will yield 123 and 456.
    """
    if isinstance(json_input, dict):
        for k, v in json_input.items():
            if k == lookup_key:
                yield v
            else:
                yield from item_generator(v, lookup_key)
    elif isinstance(json_input, list):
        for item in json_input:
            yield from item_generator(item, lookup_key)

def key_finder(source_dict:dict, key:str) -> list:
    """
    Finds all occurrences of a specific key in a nested dictionary structure.
    
    This function uses the item_generator to search through a nested dictionary
    and collect all values associated with a specific key into a list format.
    
    Args:
        source_dict (dict): The dictionary to search through
        key (str): The key to search for in the dictionary
    
    Returns:
        list: A list of dictionaries, each containing the key and its associated value
    
    Example:
        For source_dict={"a": {"card-id": 123}} and key="card-id",
        returns [{"card-id": 123}]
    """
    output = []
    for value in item_generator(source_dict, key):
        output.append({key: value})
    return output

def getTableName(session, id) -> str:
    """
    Retrieves the name of a table from Metabase using its ID.
    
    This function handles two cases:
    1. If the ID is a string, it simply capitalizes and returns it
    2. If the ID is numeric, it makes an API call to get the table metadata
    
    Args:
        session: The authenticated Metabase session object
        id: The table identifier (can be string or numeric)
    
    Returns:
        str: The capitalized table name
    """
    # table name can be an id or another Card, so that's why we check for the string type
    if isinstance(id, str):
        return f"{id.capitalize()}"
    
    try:
        table_metadata = _make_api_request(session, f"{table_url}/{id}", f"fetching table name", str(id))
        return f"{table_metadata['name']}"
    except Exception:
        return f"Unknown_Table_{id}"

def getCollectionMetadata(session, collection_id, dashboards: bool = False) -> dict:
    """
    Retrieves metadata for items within a specific Metabase collection.
    
    Args:
        session: The authenticated Metabase session object
        collection_id: The collection identifier ('root' for root collection or numeric ID)
        dashboards: If True, fetches dashboard metadata; otherwise fetches card/dataset metadata
    
    Returns:
        dict: JSON response containing the collection items metadata
    """
    if dashboards:
        item_url = f"{host}/api/collection/{collection_id}/items?models=dashboard"
    else:
        item_url = f"{host}/api/collection/{collection_id}/items?models=dataset&models=card"
    
    return _make_api_request(session, item_url, f"fetching collection metadata", str(collection_id))

def getCollectionsMetadata(session, skip_archived: bool) -> list:
    """
    Retrieves metadata for all collections in the Metabase instance.
    
    Args:
        session: The authenticated Metabase session object
        skip_archived: If True, archived collections will be excluded from results
    
    Returns:
        list: A list of dictionaries containing collection metadata
    """
    collections = []
    response_data = _make_api_request(session, collections_url, "fetching collections metadata")
    
    for collection in response_data:
        if skip_archived and collection.get('archived', False):
            continue
        slug = 'root' if collection['id'] == 'root' else collection['slug']
        collections.append({
            'id': collection['id'], 
            'name': collection['name'], 
            'slug': slug
        })
    
    return collections

def _replace_template_tags(query: str, template_tags: dict, replacements: dict | None = None) -> str:
    """
    Replaces template tags in a query string with appropriate replacements.
    
    Args:
        query: The query string containing template tags
        template_tags: Dictionary of template tags
        replacements: Optional dictionary of specific replacements for tags
    
    Returns:
        str: The query with template tags replaced
    """
    if replacements is None:
        replacements = {}
    
    for tag_name in template_tags:
        replacement = replacements.get(tag_name, 'parameter')
        # Handle both {{tag}} and {{ tag }} formats
        query = query.replace(f'{{{{{tag_name}}}}}', replacement)
        query = query.replace(f'{{{{{ {tag_name} }}}}}', replacement)
    
    return query

def _clean_query(query: str) -> str:
    """
    Cleans up a query by removing optional clause markers.
    
    Args:
        query: The query string to clean
    
    Returns:
        str: The cleaned query
    """
    return query.replace('[[', '').replace(']]', '')

def _parse_sql_tables(query: str, card_id: int) -> list:
    """
    Parses SQL query to extract table names.
    
    Args:
        query: The SQL query string
        card_id: The card ID for error reporting
    
    Returns:
        list: List of source dictionaries with sql-source-table keys
    """
    sources = []
    try:
        for table in parse_one(query).find_all(exp.Table):
            if table.name not in ['dummy', 'parameter']:
                sources.append({'sql-source-table': table.name})
    except Exception as e:
        print(f"Could not parse SQL query for card {card_id}: {e}")
    return sources

def _process_native_query(session, card_id, dataset_query):
    """
    Processes a native SQL query from a card to extract source dependencies.
    
    This helper function handles native queries, including those with template tags
    like snippets, cards, and dimensions, by calling the appropriate resolution APIs.
    
    Args:
        session: The authenticated Metabase session object.
        card_id (int): The ID of the card being processed.
        dataset_query (dict): The dataset_query object from the card's metadata.
        
    Returns:
        list: A list of source dependency dictionaries.
    """
    sources = []
    native_query_info = dataset_query.get('native', {})
    query_to_parse = native_query_info.get('query', '')
    template_tags = native_query_info.get('template-tags', {})

    if not query_to_parse:
        return []

    # Handle template tags
    if template_tags:
        # Snippets require resolving the entire query at once
        if any(tag.get('type') == 'snippet' for tag in template_tags.values()):
            resolved_query = _resolveSnippetQuery(session, dataset_query['database'], card_id)
            if resolved_query:
                query_to_parse = resolved_query
            else: # Fallback if snippet resolution fails
                print(f"Could not resolve snippet query for card {card_id}. Parsing with placeholders.")
                query_to_parse = _replace_template_tags(query_to_parse, template_tags, {tag: 'parameter' for tag in template_tags})
        elif any(tag.get('type') == 'card' for tag in template_tags.values()):
            # Handle card template tags
            # First, extract card dependencies from template tags
            for tag_name, tag_info in template_tags.items():
                if tag_info.get('type') == 'card' and 'card-id' in tag_info:
                    referenced_card_id = tag_info['card-id']
                    sources.append({'source-table': f"card__{referenced_card_id}"})
                    sources.extend(_resolveCardDependencies(session, referenced_card_id))
            
            # Then resolve the entire query to get the full SQL with card template tags expanded
            resolved_query = _resolveCardTemplateQuery(session, dataset_query, card_id)
            if resolved_query:
                query_to_parse = resolved_query
            else:
                print(f"Could not resolve card template query for card {card_id}. Parsing with placeholders.")
                # Fallback to processing individual tags
                for tag_name, tag_info in template_tags.items():
                    tag_type = tag_info.get('type')
                    if tag_type == 'card':
                        query_to_parse = _replace_template_tags(query_to_parse, {tag_name: template_tags[tag_name]}, {tag_name: 'dummy'})
                    else:
                        query_to_parse = _replace_template_tags(query_to_parse, {tag_name: template_tags[tag_name]}, {tag_name: 'parameter'})
        else:
            # Process other template tags normally
            for tag_name, tag_info in template_tags.items():
                tag_type = tag_info.get('type')
                if tag_type == 'dimension':
                    # Field filters can sometimes be resolved to find tables
                    resolved_query = _resolveFieldFilterQuery(session, dataset_query, tag_name, tag_info)
                    if resolved_query:
                        sources.extend(_parse_sql_tables(resolved_query, card_id))
                    # Replace with a placeholder for the main parse
                    query_to_parse = _replace_template_tags(query_to_parse, {tag_name: template_tags[tag_name]}, {tag_name: 'parameter'})
                else: # Other filter types
                    query_to_parse = _replace_template_tags(query_to_parse, {tag_name: template_tags[tag_name]}, {tag_name: 'parameter'})

    # Clean up optional clauses and parse the final query
    query_to_parse = _clean_query(query_to_parse)
    sources.extend(_parse_sql_tables(query_to_parse, card_id))
        
    return sources

def _process_gui_query(session, card_id, card_metadata):
    """
    Processes a GUI-based query from a card to extract source dependencies.
    
    This helper function handles questions created via the Metabase query builder,
    including those that use other cards as their source.
    
    Args:
        session: The authenticated Metabase session object.
        card_id (int): The ID of the card being processed.
        card_metadata (dict): The full metadata of the card.
        
    Returns:
        list: A list of source dependency dictionaries.
    """
    sources = []
    gui_sources = key_finder(card_metadata, 'source-table')
    for source in gui_sources:
        source_table = source.get('source-table')
        sources.append(source) # Add the direct source
        
        # If the source is another card, resolve its dependencies recursively
        if isinstance(source_table, str) and source_table.startswith('card__'):
            try:
                referenced_card_id = int(source_table.replace('card__', ''))
                sources.extend(_resolveCardDependencies(session, referenced_card_id))
            except (ValueError, TypeError) as e:
                print(f"Invalid card ID format in GUI source '{source_table}' for card {card_id}: {e}")
    return sources

def getSourcesFromCard(session, id:int) -> dict:
    """
    Extracts source table dependencies from a Metabase card (question).
    
    This function analyzes a Metabase card to determine its data sources by handling
    both native SQL queries and GUI-created questions. It intelligently parses SQL,
    resolves template tags (snippets, cards, dimensions), and recursively follows
    card-to-card dependencies to build a complete lineage.
    
    Args:
        session: The authenticated Metabase session object.
        id (int): The card ID to analyze.
    
    Returns:
        dict: A dictionary containing card metadata and its deduplicated sources.
              Returns a placeholder for deleted or inaccessible cards.
    """
    default_card_info = {
        "card_name": 'Corrupted or inaccessible card', "card_id": str(id),
        'collection_slug': 'root', 'card_sources': [], 'archived': True
    }
    try:
        response = session.get(f"{card_url}/{id}", verify=False)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else 500
        if status_code == 401:
            print(f"Authentication failed: Invalid or expired session token.")
            raise typer.Exit(code=1)
        elif status_code == 404:
            default_card_info["card_name"] = 'Deleted or inaccessible card'
        else:
            print(f"HTTP error {status_code} while fetching card {id}: {e}")
        return default_card_info

    try:
        card_metadata = response.json()
        dataset_query = card_metadata.get('dataset_query', {})
        card_sources = []

        if 'native' in dataset_query:
            card_sources = _process_native_query(session, id, dataset_query)
        else:
            card_sources = _process_gui_query(session, id, card_metadata)

        return {
            'card_sources': _deduplicateSources(card_sources),
            'card_id': str(card_metadata['id']),
            'database': dataset_query.get('database'),
            'card_name': card_metadata.get('name', f'Untitled Card {id}'),
            'collection_slug': 'root' if card_metadata.get('collection', {}).get('id') == 'root' else card_metadata.get('collection', {}).get('slug', 'root'),
            'archived': card_metadata.get('archived', False)
        }
    except Exception as e:
        print(f"Unexpected error while processing card {id}: {e}")
        return default_card_info

def getSchemas(session, database: int) -> dict:
    """
    Retrieves all schemas for a specific database from Metabase.
    
    Args:
        session: The authenticated Metabase session object
        database: The database ID to fetch schemas for
    
    Returns:
        dict: JSON response containing schema information for the database
    """
    return _make_api_request(session, f"{databases_url}/{database}/schemas", "fetching schemas", str(database))

def getDashboardMetadata(session, dashboard: int) -> dict:
    """
    Retrieves metadata for a specific dashboard from Metabase.
    
    Args:
        session: The authenticated Metabase session object
        dashboard: The dashboard ID to fetch metadata for
    
    Returns:
        dict: JSON response containing dashboard metadata including cards and settings
    """
    try:
        return _make_api_request(session, f"{dashboard_url}/{dashboard}", "fetching dashboard", str(dashboard))
    except:
        return {}  # Return empty dict for missing dashboards

def getDashboardCards(session, dashboard: int) -> list:
    """
    Retrieves the cards (questions) contained within a specific dashboard.
    
    Args:
        session: The authenticated Metabase session object
        dashboard: The dashboard ID to fetch cards for
    
    Returns:
        list: List of dashboard cards (dashcards)
    """
    try:
        dashboard_metadata = _make_api_request(session, f"{dashboard_url}/{dashboard}", "fetching dashboard cards", str(dashboard))
        return dashboard_metadata.get("dashcards", [])
    except:
        return []  # Return empty list for missing dashboards

def getDatabases(session) -> dict:
    """
    Retrieves all databases from Metabase, excluding unsupported types.
    
    Args:
        session: The authenticated Metabase session object
    
    Returns:
        dict: A dictionary mapping database IDs to database names
    """
    databases = {}
    databases_metadata = _make_api_request(session, databases_url, "fetching databases")
    
    for database in databases_metadata["data"]:
        # H2 and Mongo are not supported
        if database["engine"] in ["h2", "mongo"]:
            continue
        databases[database["id"]] = database["name"]
    
    return databases

def getTables(session, database: int, schema: str) -> dict:
    """
    Retrieves all tables for a specific database schema from Metabase.
    
    Args:
        session: The authenticated Metabase session object
        database: The database ID
        schema: The schema name (will be URL encoded if contains '/')
    
    Returns:
        dict: A dictionary mapping table IDs to table names
    """
    tables = {}
    if "/" in schema:
        schema = schema.replace("/", "%2F")
    
    try:
        tables_metadata = _make_api_request(session, f"{databases_url}/{database}/schema/{schema}", "fetching tables", f"database {database}, schema {schema}")
        for table in tables_metadata:
            tables[table["id"]] = table["name"]
        return tables
    except Exception as e:
        print(f"Error processing tables for database {database}, schema {schema}: {e}")
        return {}

def getFields(session, table: int) -> dict:
    """
    Retrieves all fields (columns) for a specific table from Metabase.
    
    Args:
        session: The authenticated Metabase session object
        table: The table ID to fetch fields for
    
    Returns:
        dict: A dictionary mapping field IDs to field names
    """
    fields = {}
    fields_metadata = _make_api_request(session, f"{table_url}/{table}/query_metadata", "fetching fields", str(table))
    
    for field in fields_metadata['fields']:
        fields[field['id']] = field['name']
    
    return fields

def _create_cypher_node(node_type: str, identifier: str, properties: dict) -> str:
    """
    Creates a Cypher CREATE statement for a node.
    
    Args:
        node_type: The type/label of the node
        identifier: The unique identifier for the node
        properties: Dictionary of properties for the node
    
    Returns:
        str: The Cypher CREATE statement
    """
    props_str = ', '.join([f"{k}:'{v}'" for k, v in properties.items()])
    return f"CREATE ({identifier}:{node_type} {{{props_str}}})\n"

def _create_cypher_relationship(from_node: str, to_node: str, relationship: str, from_props: dict | None = None, to_props: dict | None = None) -> str:
    """
    Creates a Cypher relationship statement.
    
    Args:
        from_node: The source node type/label
        to_node: The target node type/label
        relationship: The relationship type
        from_props: Properties to match the source node
        to_props: Properties to match the target node
    
    Returns:
        str: The Cypher MATCH and CREATE relationship statement
    """
    if from_props is None:
        from_props = {}
    if to_props is None:
        to_props = {}
    
    from_props_str = ', '.join([f"{k}:'{v}'" for k, v in from_props.items()])
    to_props_str = ', '.join([f"{k}:'{v}'" for k, v in to_props.items()])
    
    match_stmt = f"MATCH (a_{from_node.lower()}:{from_node} {{{from_props_str}}}), (a_{to_node.lower()}:{to_node} {{{to_props_str}}})\n"
    create_stmt = f"CREATE (a_{from_node.lower()})-[:{relationship}]->(a_{to_node.lower()})\n"
    
    return match_stmt + create_stmt

def _should_skip_item(item: dict, skip_archived: bool, database_list: list | None = None) -> bool:
    """
    Determines if an item should be skipped based on filtering criteria.
    
    Args:
        item: The item to check
        skip_archived: Whether to skip archived items
        database_list: List of allowed database IDs
    
    Returns:
        bool: True if the item should be skipped
    """
    if skip_archived and item.get('archived', False):
        return True
    
    if database_list and item.get('database') not in database_list:
        return True
    
    return False

def sanitize(string: str) -> str:
    """
    Sanitizes a string by replacing special characters with underscores.
    
    This function is used to clean strings before using them in Cypher queries
    or as node identifiers, ensuring they don't contain characters that could
    cause syntax errors or issues in Neo4j.
    
    Args:
        string: The string to sanitize
    
    Returns:
        str: The sanitized string with special characters replaced by underscores
    """
    for ch in [' ', "'", "(", ")", "{", "}", "/", "\\", "-", "@", ".", "?", "[", "]", ":", ";", ",", "!", "#", "$", "%", "^", "&", "*", "+", "=", "<", ">", "|", "~", "`"]:
        string = string.replace(ch, "_")
    return string
    """
    Sanitizes a string by replacing special characters with underscores.
    
    This function is used to clean strings before using them in Cypher queries
    or as node identifiers, ensuring they don't contain characters that could
    cause syntax errors or issues in Neo4j.
    
    Args:
        string (str): The string to sanitize
    
    Returns:
        str: The sanitized string with special characters replaced by underscores
    
    Note:
        The following characters are replaced with underscores:
        space, ', (, ), {, }, /, \\, -, @, ., ?, [, ], :, ;, ,, !, #, $, %, ^, &, *, +, =, <, >, |, ~, `
    """
    for ch in [' ', "'", "(", ")", "{", "}", "/", "\\", "-", "@", ".", "?", "[", "]", ":", ";", ",", "!", "#", "$", "%", "^", "&", "*", "+", "=", "<", ">", "|", "~", "`"]:
        string = string.replace(ch, "_")
    return string

def _write_cypher(writer_type: str, writer, query: str) -> None:
    """
    Writes Cypher syntax to either a Neo4j database or a file.
    
    Args:
        writer_type: The type of writer ('neo4j' for Neo4j, 'file' for file output)
        writer: The writer object (Neo4j driver or file handle)
        query: The Cypher query string to write
    """
    try:
        if writer_type == 'neo4j':
            writer.execute_query(query, database="neo4j")
        elif writer_type == 'file':
            writer.write(query)
    except Exception as e:
        context = "Neo4j database" if writer_type == 'neo4j' else "file"
        print(f"Error writing to {context}: {e}")
        if writer_type == 'neo4j':
            print(f"Query that failed: {query[:100]}...")
        # Don't raise to allow processing to continue

def writeDatabases(session, writer_type, writer, fields: bool, database_list: list) -> None:
    """
    Writes database, schema, table, and optionally field metadata to Neo4j or file.
    
    Args:
        session: The authenticated Metabase session object
        writer_type: The type of writer ('neo4j' for Neo4j, 'file' for file output)
        writer: The writer object (Neo4j driver or file handle)
        fields: Whether to include field-level metadata
        database_list: List of database IDs to process (empty list processes all)
    """
    databases = getDatabases(session)
    # Filter databases based on database_list
    filtered_databases = {db_id: db_name for db_id, db_name in databases.items() 
                         if not database_list or db_id in database_list}
    
    # Writing databases, schemas, tables (and fields)
    with typer.progressbar(filtered_databases.keys(), label="Writing databases") as progress:
        for database in filtered_databases:
            db_name = sanitize(filtered_databases[database])
            create_database = _create_cypher_node("Database", f"{db_name}{database}", 
                                               {"name": db_name, "key": f"db{database}"})
            _write_cypher(writer_type, writer, create_database)
            
            schemas = getSchemas(session, database)
            for schema in schemas:
                schema_name = sanitize(schema)
                create_schema = _create_cypher_node("Schema", f"{schema_name}{database}", 
                                                 {"name": schema})
                _write_cypher(writer_type, writer, create_schema)
                
                # Create relationship between database and schema
                db_schema_rel = _create_cypher_relationship("Database", "Schema", "BELONGS_TO",
                                                          {"key": f"db{database}"}, {"name": schema})
                _write_cypher(writer_type, writer, db_schema_rel)
                
                tables = getTables(session, database, schema)
                for table_id, table_name in tables.items():
                    table_name_clean = sanitize(table_name)
                    create_table = _create_cypher_node("Table", f"{table_name_clean}{table_id}", 
                                                     {"name": table_name_clean, "key": f"table{table_id}"})
                    _write_cypher(writer_type, writer, create_table)
                    
                    # Create relationship between schema and table
                    schema_table_rel = _create_cypher_relationship("Table", "Schema", "BELONGS_TO",
                                                                 {"key": f"table{table_id}"}, {"name": schema})
                    _write_cypher(writer_type, writer, schema_table_rel)
                    
                    if fields:
                        table_fields = getFields(session, table_id)
                        for field_id, field_name in table_fields.items():
                            field_name_clean = sanitize(field_name)
                            create_field = _create_cypher_node("Field", f"{field_name_clean}{field_id}", 
                                                             {"name": field_name_clean, "key": f"field{field_id}"})
                            _write_cypher(writer_type, writer, create_field)
                            
                            # Create relationship between table and field
                            table_field_rel = _create_cypher_relationship("Field", "Table", "BELONGS_TO",
                                                                        {"key": f"field{field_id}"}, {"key": f"table{table_id}"})
                            _write_cypher(writer_type, writer, table_field_rel)
            progress.update(1)

def writeCollectionsAndCards(session, writer_type, writer, skip_archived: bool, database_list: list) -> None:
    """
    Fetches collections and cards and writes them as nodes and relationships.
    
    Args:
        session: The active session object for making API requests
        writer_type: The type of writer to use ('file', 'neo4j')
        writer: The writer instance
        skip_archived: If True, archived cards and collections will be ignored
        database_list: List of database IDs to filter by
    """
    collections = getCollectionsMetadata(session, skip_archived)
    max_card_id = 0
    
    # Phase 1: Write collections and find max card ID
    with typer.progressbar(collections, label="Writing collections") as progress:
        for collection in progress:
            cards_metadata = getCollectionMetadata(session, collection['id'])
            collection_name = sanitize(collection['name'])
            create_collection = _create_cypher_node("Collection", f"{collection_name}{collection['id']}", 
                                                   {"name": collection['slug'], "key": f"collection{collection['id']}"})
            _write_cypher(writer_type, writer, create_collection)
            
            # Find the maximum card ID for progress tracking in phase 2
            for card in cards_metadata["data"]:
                if card['id'] > max_card_id:
                    max_card_id = card['id']
    
    # Phase 2: Write cards with proper progress tracking
    with typer.progressbar(range(max_card_id), label="Writing cards") as progress:
        for card_id in range(1, max_card_id + 1):
            card_metadata = getSourcesFromCard(session, card_id)
            
            # Apply filters
            if _should_skip_item(card_metadata, skip_archived, database_list):
                progress.update(1)
                continue
            
            # Create card node and relationships
            card_name = sanitize(card_metadata['card_name'])
            create_card = _create_cypher_node("Card", f"Card__{card_metadata['card_id']}", 
                                            {"name": card_name, "key": f"card{card_metadata['card_id']}"})
            _write_cypher(writer_type, writer, create_card)
            
            # Create relationship to collection
            card_collection_rel = _create_cypher_relationship("Card", "Collection", "BELONGS_TO",
                                                            {"key": f"card{card_metadata['card_id']}"}, 
                                                            {"name": card_metadata['collection_slug']})
            _write_cypher(writer_type, writer, card_collection_rel)
            
            # Process card sources
            for source in card_metadata.get('card_sources', []):
                if 'sql-source-table' in source:
                    # SQL source table
                    source_rel = _create_cypher_relationship("Card", "Table", "SOURCE",
                                                           {"key": f"card{card_metadata['card_id']}"}, 
                                                           {"name": source['sql-source-table'].capitalize()})
                elif 'source-table' in source:
                    source_table = source['source-table']
                    if isinstance(source_table, str) and source_table.startswith('card__'):
                        # Card-to-card relationship
                        source_rel = _create_cypher_relationship("Card", "Card", "SOURCE",
                                                               {"key": f"card{card_metadata['card_id']}"}, 
                                                               {"key": source_table.lower().replace('__', '')})
                    else:
                        # Regular table relationship
                        table_name = sanitize(getTableName(session, source_table))
                        source_rel = _create_cypher_relationship("Card", "Table", "SOURCE",
                                                               {"key": f"card{card_metadata['card_id']}"}, 
                                                               {"name": table_name})
                else:
                    continue
                    
                _write_cypher(writer_type, writer, source_rel)
            
            progress.update(1)

def writeDashboards(session, writer_type, writer, skip_archived: bool) -> None:
    """
    Writes dashboard metadata and their card relationships to Neo4j or file.
    
    Args:
        session: The authenticated Metabase session object
        writer_type: The type of writer ('neo4j' for Neo4j, 'file' for file output)
        writer: The writer object (Neo4j driver or file handle)
        skip_archived: Whether to skip archived dashboards
    """
    collections = getCollectionsMetadata(session, skip_archived)
    
    # First, collect all dashboards to get accurate count
    all_dashboards = []
    for collection in collections:
        dashboards_metadata = getCollectionMetadata(session, collection['id'], dashboards=True)
        for dashboard_info in dashboards_metadata["data"]:
            dashboard = getDashboardMetadata(session, dashboard_info['id'])
            if dashboard and not _should_skip_item(dashboard, skip_archived):
                all_dashboards.append(dashboard)
    
    # Now process dashboards with accurate progress tracking
    with typer.progressbar(all_dashboards, label="Writing dashboards") as progress:
        for dashboard in progress:
            dashboard_name = sanitize(dashboard['name'])
            create_dashboard = _create_cypher_node("Dashboard", f"{dashboard_name}{dashboard['id']}", 
                                                 {"name": dashboard_name, "key": f"dashboard{dashboard['id']}"})
            _write_cypher(writer_type, writer, create_dashboard)
            
            dashboard_cards = getDashboardCards(session, dashboard["id"])
            for card in dashboard_cards:
                card_id = card.get('card', {}).get('id') or card.get('id')
                if card_id:
                    dashboard_card_rel = _create_cypher_relationship("Dashboard", "Card", "CONTAINS",
                                                                   {"key": f"dashboard{dashboard['id']}"}, 
                                                                   {"key": f"card{card_id}"})
                    _write_cypher(writer_type, writer, dashboard_card_rel)

def _resolveSnippetQuery(session, database_id: int, card_id: int) -> str | None:
    """
    Resolves a SQL snippet by calling the native API to get the compiled native form.
    
    This function calls the native dataset endpoint with the card ID as source-card
    to get the fully compiled query with all SQL snippets resolved.
    
    Args:
        session: The authenticated Metabase session object
        database_id (int): The database ID
        card_id (int): The card ID containing the snippet to be resolved
    
    Returns:
        str | None: The resolved SQL query, or None if resolution fails
    """
    try:
        payload = {
            "database": database_id,
            "type": "query",
            "query": {
                "source-card": card_id
            }
        }
        
        response = session.post(native_card_url, json=payload, verify=False)
        response.raise_for_status()
        
        result = response.json()
        # The native API returns the resolved query in the response
        if 'data' in result and 'native_form' in result['data']:
            return result['data']['native_form'].get('query', '')
        elif 'query' in result:
            return result['query']
        else:
            return None
    except Exception as e:
        print(f"Error resolving snippet query for card {card_id}: {e}")
        return None

def _resolveFieldFilterQuery(session, dataset_query: dict, tag_name: str, tag_info: dict) -> str | None:
    """
    Resolves a field filter query by calling the native API with the template tag structure.
    
    Args:
        session: The authenticated Metabase session object
        dataset_query (dict): The dataset query containing the native query and template tags
        tag_name (str): The name of the template tag
        tag_info (dict): The template tag information
    
    Returns:
        str | None: The resolved SQL query, or None if resolution fails
    """
    try:
        # Build the payload for the native API call
        payload = {
            "database": dataset_query['database'],
            "type": "native",
            "native": {
                "template-tags": {
                    tag_name: tag_info
                },
                "query": dataset_query['native']['query']
            },
            "parameters": [
                {
                    "id": tag_info.get('id', ''),
                    "type": tag_info.get('widget-type', 'string/='),
                    "value": None,
                    "target": ["dimension", ["template-tag", tag_name]]
                }
            ],
            "pretty": False
        }
        
        response = session.post(native_card_url, json=payload, verify=False)
        response.raise_for_status()
        
        result = response.json()
        # The native API returns the resolved query in the response
        if 'data' in result and 'native_form' in result['data']:
            return result['data']['native_form'].get('query', '')
        elif 'query' in result:
            return result['query']
        else:
            return None
    except Exception as e:
        print(f"Error resolving field filter query for tag {tag_name}: {e}")
        return None

def _resolveCardDependencies(session, card_id: int, visited_cards: set | None = None, max_depth: int = 10) -> list:
    """
    Recursively resolves card dependencies to find all source tables and cards.
    
    This function follows the chain of card dependencies to find the ultimate
    source tables. For example: Card A -> Card B -> Card C -> Table X will
    return both the card dependencies and the table dependency.
    
    Args:
        session: The authenticated Metabase session object
        card_id (int): The card ID to resolve dependencies for
        visited_cards (set): Set of already visited card IDs to prevent infinite loops
        max_depth (int): Maximum recursion depth to prevent stack overflow
    
    Returns:
        list: List of source dependencies (both cards and tables)
    """
    if visited_cards is None:
        visited_cards = set()
    
    # Prevent infinite loops and stack overflow
    if card_id in visited_cards or max_depth <= 0:
        return []
    
    visited_cards.add(card_id)
    all_sources = []
    
    try:
        # Get the card metadata
        card_metadata = getSourcesFromCard(session, card_id)
        
        if card_metadata.get('archived', False):
            return []
            
        for source in card_metadata.get('card_sources', []):
            if 'source-table' in source:
                source_table = source['source-table']
                
                # If it's a card reference (starts with 'card__')
                if isinstance(source_table, str) and source_table.startswith('card__'):
                    # Extract the card ID
                    try:
                        referenced_card_id = int(source_table.replace('card__', ''))
                        
                        # Add the direct card dependency
                        all_sources.append(source)
                        
                        # Recursively resolve the referenced card's dependencies
                        nested_sources = _resolveCardDependencies(session, referenced_card_id, visited_cards.copy(), max_depth - 1)
                        all_sources.extend(nested_sources)
                    except (ValueError, TypeError) as e:
                        print(f"Invalid card ID format in {source_table}: {e}")
                else:
                    # It's a regular table reference
                    all_sources.append(source)
            elif 'sql-source-table' in source:
                # Direct table reference from SQL
                all_sources.append(source)
    
    except Exception as e:
        print(f"Error resolving dependencies for card {card_id}: {e}")
    
    return all_sources

def _deduplicateSources(sources: list) -> list:
    """
    Removes duplicate sources from a list while preserving order.
    
    Args:
        sources (list): List of source dictionaries
    
    Returns:
        list: Deduplicated list of sources
    """
    seen = set()
    deduplicated = []
    
    for source in sources:
        # Create a hashable representation of the source
        if 'source-table' in source:
            key = ('source-table', str(source['source-table']))
        elif 'sql-source-table' in source:
            key = ('sql-source-table', str(source['sql-source-table']))
        else:
            # Fallback for unknown source types
            key = tuple(sorted(source.items()))
        
        if key not in seen:
            seen.add(key)
            deduplicated.append(source)
    
    return deduplicated

@app.command()
def cypher(fields: bool = False, skip_archived: bool = True, database_list: str = ""):
    """
    Export Metabase metadata to a Cypher file for Neo4j import.
    """
    parsed_database_list = [int(e) if e.isdigit() else e for e in database_list.split(',') if e.strip()]
    metabase_session = metabaseAuth()
    try:
        with open('metadata.cypher', 'w', encoding='utf-8') as writer:
            writeDatabases(metabase_session, 'file', writer, fields, parsed_database_list)
            writeCollectionsAndCards(metabase_session, 'file', writer, skip_archived, parsed_database_list)
            writeDashboards(metabase_session, 'file', writer, skip_archived)
    except Exception as e:
        print(f"Error during cypher export: {e}")
        raise typer.Exit(code=1)
    finally:
        _cleanup_session(metabase_session)

@app.command()
def neo4j(fields: bool = False, skip_archived: bool = True, database_list: str = ""):
    """
    Export Metabase metadata directly to a Neo4j database.
    """
    parsed_database_list = [int(e) if e.isdigit() else e for e in database_list.split(',') if e.strip()]
    metabase_session = metabaseAuth()
    writer = None
    try:
        writer = dbAuth()
        writeDatabases(metabase_session, 'neo4j', writer, fields, parsed_database_list)
        writeCollectionsAndCards(metabase_session, 'neo4j', writer, skip_archived, parsed_database_list)
        writeDashboards(metabase_session, 'neo4j', writer, skip_archived)
    except Exception as e:
        print(f"Error during neo4j export: {e}")
        raise typer.Exit(code=1)
    finally:
        _cleanup_neo4j_driver(writer)
        _cleanup_session(metabase_session)

@app.command()
def database(fields: bool = False, database_list: str = ""):
    """
    Export only database structure metadata to a Cypher file.
    """
    parsed_database_list = [int(e) if e.isdigit() else e for e in database_list.split(',') if e.strip()]
    metabase_session = metabaseAuth()
    try:
        with open('metadata.cypher', 'w', encoding='utf-8') as writer:
            writeDatabases(metabase_session, 'file', writer, fields, parsed_database_list)
    except Exception as e:
        print(f"Error during database export: {e}")
        raise typer.Exit(code=1)
    finally:
        _cleanup_session(metabase_session)

def _cleanup_session(session):
    """Clean up the Metabase session."""
    try:
        session.delete(login_url)
    except Exception:
        pass  # Ignore errors during cleanup

def _cleanup_neo4j_driver(driver):
    """Clean up the Neo4j driver."""
    if driver:
        try:
            driver.close()
        except Exception:
            pass  # Ignore errors during cleanup

def _resolveCardTemplateQuery(session, dataset_query: dict, card_id: int) -> str | None:
    """
    Resolves a query with card template tags by calling the native API to get the compiled native form.
    
    This function calls the native dataset endpoint with the dataset query to get the fully
    compiled query with all card template tags resolved to their actual SQL.
    
    Args:
        session: The authenticated Metabase session object
        dataset_query (dict): The dataset query containing the native query and template tags
        card_id (int): The card ID for error reporting
    
    Returns:
        str | None: The resolved SQL query, or None if resolution fails
    """
    try:
        payload = {
            "database": dataset_query['database'],
            "type": "native",
            "native": dataset_query['native'],
            "pretty": False
        }
        
        response = session.post(native_card_url, json=payload, verify=False)
        response.raise_for_status()
        
        result = response.json()
        # The native API returns the resolved query in the response
        if 'data' in result and 'native_form' in result['data']:
            return result['data']['native_form'].get('query', '')
        elif 'query' in result:
            return result['query']
        else:
            return None
    except Exception as e:
        print(f"Error resolving card template query for card {card_id}: {e}")
        return None

@app.command()
def test_card(card_id: int, verbose: bool = False):
    """
    Test parsing a specific card to see its source dependencies.
    
    Args:
        card_id: The ID of the card to test
        verbose: If True, prints detailed information about the parsing process
    """
    metabase_session = metabaseAuth()
    try:
        print(f"Testing card {card_id}...")
        
        # Get the card metadata
        card_metadata = getSourcesFromCard(metabase_session, card_id)
        
        if card_metadata.get('archived', False):
            print(f"⚠️  Card {card_id} is archived")
        
        print(f"Card Name: {card_metadata.get('card_name', 'Unknown')}")
        print(f"Collection: {card_metadata.get('collection_slug', 'Unknown')}")
        print(f"Database: {card_metadata.get('database', 'Unknown')}")
        
        sources = card_metadata.get('card_sources', [])
        if sources:
            print(f"\nFound {len(sources)} source(s):")
            for i, source in enumerate(sources, 1):
                if 'source-table' in source:
                    source_table = source['source-table']
                    if isinstance(source_table, str) and source_table.startswith('card__'):
                        print(f"  {i}. Card dependency: {source_table}")
                    else:
                        print(f"  {i}. Table: {source_table}")
                elif 'sql-source-table' in source:
                    print(f"  {i}. SQL table: {source['sql-source-table']}")
                else:
                    print(f"  {i}. Unknown source type: {source}")
        else:
            print("No sources found")
        
        if verbose:
            print("\nDetailed card metadata:")
            print(f"Raw card_sources: {sources}")
            
            # Get the raw card data for more details
            try:
                response = metabase_session.get(f"{card_url}/{card_id}", verify=False)
                response.raise_for_status()
                raw_card = response.json()
                
                dataset_query = raw_card.get('dataset_query', {})
                print(f"\nDataset query type: {'native' if 'native' in dataset_query else 'gui'}")
                
                if 'native' in dataset_query:
                    native_info = dataset_query['native']
                    print(f"Native query length: {len(native_info.get('query', ''))}")
                    template_tags = native_info.get('template-tags', {})
                    if template_tags:
                        print(f"Template tags found: {len(template_tags)}")
                        for tag_name, tag_info in template_tags.items():
                            tag_type = tag_info.get('type', 'unknown')
                            print(f"  - {tag_name}: {tag_type}")
                            if tag_type == 'card' and 'card-id' in tag_info:
                                print(f"    -> References card {tag_info['card-id']}")
                    else:
                        print("No template tags found")
                        
                    # Show a snippet of the query
                    query = native_info.get('query', '')
                    if query:
                        print(f"\nQuery snippet (first 200 chars):")
                        print(f"'{query[:200]}{'...' if len(query) > 200 else ''}'")
                
            except Exception as e:
                print(f"Error getting raw card data: {e}")
        
        print(f"\n✅ Card {card_id} processed successfully")
        
    except Exception as e:
        print(f"❌ Error testing card {card_id}: {e}")
        raise typer.Exit(code=1)
    finally:
        _cleanup_session(metabase_session)

if __name__ == "__main__":
    app()
