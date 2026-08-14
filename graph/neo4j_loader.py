from neo4j import GraphDatabase

URI = "neo4j://127.0.0.1:7687"
USER = "neo4j"
PASSWORD = "repo12345"
DATABASE = "repo-intelligence"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))

def test_connection():
    with driver.session(database=DATABASE) as session:
        result = session.run("RETURN 'Connected to Repo Intelligence' AS message")
        print(result.single()["message"])

def create_file_node(file_path: str):
    with driver.session(database=DATABASE) as session:
        session.run(
            "MERGE (f:File {path: $path})",
            path=file_path
        )
        print(f"Created File node: {file_path}")

def create_function_node(file_path: str, function_name: str):
    with driver.session(database=DATABASE) as session:
        session.run(
            """
            MERGE (f:File {path: $file_path})
            MERGE (fn:Function {name: $function_name})
            MERGE (f)-[:CONTAINS]->(fn)
            """,
            file_path=file_path,
            function_name=function_name
        )
        print(f"Linked {file_path} -> {function_name}")

if __name__ == "__main__": 
    test_connection() 
    create_function_node("cli/main.py", "health")