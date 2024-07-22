import os
import openai
import pywikibot
import networkx as nx
import matplotlib.pyplot as plt
import re
from relations import our_relations
import requests
import numpy as np
from transformers import GPT2Tokenizer, GPT2LMHeadModel, AutoModelForCausalLM, AutoTokenizer, LlamaForCausalLM, LlamaTokenizer
import torch
from transformers import pipeline, set_seed
from queue import Queue
openai.api_key = os.getenv("OPENAI_API_KEY")
pywikibot.config.socket_timeout = 30
SITE = pywikibot.Site("wikidata", "wikidata")
REPO = SITE.data_repository()

def sanitize_input(text):
    """Remove unwanted prefixes and trim text."""
    return re.sub(r'^[\d\.\-]+\s*', '', text).strip()

def robust_request(item_id):
    """Fetch a single item from Wikidata by item ID."""
    try:
        item = pywikibot.ItemPage(REPO, item_id)
        item.get()
        print(f"Successfully fetched Wikidata item: {item_id}")
        return item if item.exists() else None
    except Exception as e:
        print(f"Failed to fetch item '{item_id}' due to error: {e}")
        return None

def fetch_label_by_id(entity_id):
    try:
        page = pywikibot.PropertyPage(REPO, entity_id) if entity_id.startswith('P') else pywikibot.ItemPage(REPO, entity_id)
        page.get(force=True)
        label = page.labels.get('en', 'No label found')
        print(f"Label for {entity_id}: {label}")
        return label
    except Exception as e:
        print(f"Error fetching label for ID {entity_id}: {e}")
        return "Invalid ID"

def paraphrase_subject(subject_label):
    prompt = (
        "Bill Clinton is also known as:\n"
        "- William Clinton\n"
        "- William Jefferson Clinton\n"
        "- The 42nd president of the United States\n"
        "\n"
        "United Kingdom is also known as:\n"
        "- UK\n"
        "- Britain\n"
        "- England\n"
        "\n"

        f"{subject_label} is also known as:"
    )
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Generate paraphrases for the subject in specific form."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    paraphrases_text = response.choices[0].message['content'].strip()
    paraphrases = re.split(r'\s*\n+', paraphrases_text)
    sanitized_paraphrases = []
    for p in paraphrases:
        if ":" in p:
            p = p.split(":")[1].strip()
        sanitized_paraphrase = sanitize_input(p)
        if is_valid_paraphrase_subject(sanitized_paraphrase):
            sanitized_paraphrases.append(sanitized_paraphrase)
    print(f"Subject paraphrases for '{subject_label}': {sanitized_paraphrases}")
    return sanitized_paraphrases

def paraphrase_relation(relation_label):
    instructions = [
        f"'{relation_label}' may be described as:",
        f"'{relation_label}' refers to:",
        "please describe '{}' in a few words:".format(relation_label)
    ]

    all_paraphrases = set()  # Use a set to avoid duplicates

    for instruction in instructions:
        prompt = (
            f"'notable work' may be described as:\n"
            "- A work of great value\n"
            "- A work of importance\n"
            "'notable work' refers to:\n"
            "- Significant achievements\n"
            "- Important contributions\n"
            "please describe 'notable work' in a few words:\n"
            "- Key accomplishments\n"
            "- Major works\n"
            "\n"
            f"{instruction}"
        )

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Generate paraphrases for the relation."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100
        )
        
        paraphrases_text = response['choices'][0]['message']['content'].strip()
        paraphrases = re.split(r'\s*\n+', paraphrases_text)
        valid_paraphrases = [sanitize_input(p) for p in paraphrases if is_valid_paraphrase_relation(p, instructions)]
        all_paraphrases.update(valid_paraphrases)  # Add to set to avoid duplicates

    print(f"Relation paraphrases for '{relation_label}': {list(all_paraphrases)}")
    return list(all_paraphrases)

def is_valid_paraphrase_relation(paraphrase, instructions):
    """Check if the generated paraphrase is valid based on some criteria."""
    paraphrase_lower = paraphrase.lower()
    invalid_phrases = ["can also be defined as", "is also known as", "is referred to as", "also known as", "please paraphrase"]
    for instr in instructions:
        instr_lower = instr.lower()
        if instr_lower in paraphrase_lower or paraphrase_lower in instr_lower or any(phrase in paraphrase_lower for phrase in invalid_phrases):
            return False
    return len(paraphrase) > 0 and not paraphrase_lower.startswith("error")


def is_valid_paraphrase_subject(paraphrase):
    valid = len(paraphrase.split()) > 1 or (len(paraphrase) > 1 and paraphrase.isalpha())
    if not valid:
        print(f"Invalid paraphrase discarded: {paraphrase}")
    return valid


def resolve_wikidata_id(paraphrases):
    wikipedia_site = pywikibot.Site('en', 'wikipedia')
    for paraphrase in paraphrases:
        print(f"Resolving paraphrase: {paraphrase}")
        search_page = wikipedia_site.search(paraphrase, total=1)
        for page in search_page:
            if page.exists():
                if page.isRedirectPage():
                    page = page.getRedirectTarget()
                if page.data_item():
                    wikidata_id = page.data_item().title()
                    print(f"Resolved to Wikidata ID: {wikidata_id} for paraphrase: {paraphrase}")
                    return wikidata_id
        print(f"No Wikidata ID found for paraphrase: {paraphrase}")
    return None

def generate_object(entity_label, relation_label, model):
    # Define the prompt
    prompt = f"""Based on the information provided, please answer the following question in strict format:
    Q: Monte Cremasco # country
    A: Italy
    Q: Johnny Depp # children
    A: Jack Depp, Lily-Rose Depp
    Q: Wolfgang Sauseng # employer
    A: University of Music and Performing Arts Vienna
    Q: {entity_label} # {relation_label}
    A:"""
    if model == 'gpt2_xl':
        gpt2_xl_tokenizer = GPT2Tokenizer.from_pretrained("gpt2-xl")
        gpt2_xl = GPT2LMHeadModel.from_pretrained("gpt2-xl", pad_token_id=gpt2_xl_tokenizer.eos_token_id)
        input_ids = gpt2_xl_tokenizer.encode(prompt, return_tensors='pt')
        max_length = input_ids.shape[1] + 50
        output = gpt2_xl.generate(input_ids, max_length=max_length)
        full_response = gpt2_xl_tokenizer.decode(output[0], skip_special_tokens=True)
        print("full response: ", full_response)

    elif model == 'gpt_j':
        gptj_tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-j-6B")
        gpt_j = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-j-6B", pad_token_id= gptj_tokenizer.eos_token_id)
        input_ids = gptj_tokenizer.encode(prompt, return_tensors='pt')
        max_length = input_ids.shape[1] + 50
        output = gpt_j.generate(input_ids, max_length=max_length)
        full_response = gptj_tokenizer.decode(output[0], skip_special_tokens=True)
        print("full response: ", full_response)
    
    elif model == 'llama2':
        model_dir = '/data/akshat/models/Llama-2-7b-hf'
        llama2_tokenizer = LlamaTokenizer.from_pretrained(model_dir)
        llama2 = LlamaForCausalLM.from_pretrained(model_dir)
        input_ids = llama2_tokenizer.encode(prompt, return_tensors='pt')
        max_length = input_ids.shape[1] + 50
        output = llama2.generate(input_ids, max_length=max_length)
        full_response = llama2_tokenizer.decode(output[0], skip_special_tokens=True)
        print("full response: ", full_response)

    # Extract the relevant answer
    query_string = f"Q: {entity_label} # {relation_label}\n    A:"
    start_index = full_response.find(query_string)
    if start_index != -1:
        start_index += len(query_string)
        end_index = full_response.find("\n    Q:", start_index)
        if end_index == -1:  # No more questions, take till end
            end_index = len(full_response)
        answer = full_response[start_index:end_index].strip()
    else:
        answer = "No answer found."

    # Print the answer for debugging
    print("Generated response:", answer)
    return answer

    
def visualize_graph(graph):
    plt.figure(figsize=(30, 30))  # Increase the figure size

    # Position nodes using a layout algorithm with spacing parameters
    pos = nx.spring_layout(graph, k=1, iterations=50)  # Adjust 'k' for distance between nodes, and 'iterations' for layout precision

    # Calculate node sizes based on label lengths
    labels = {node: node for node in graph.nodes()}
    sizes = [len(label) * 2000 for label in labels.values()]  # Adjust node size based on text length

    # Draw the nodes
    nx.draw_networkx_nodes(graph, pos, node_size=sizes, node_color='skyblue', edgecolors='black', alpha=0.6)

    # Draw the edges
    nx.draw_networkx_edges(graph, pos, arrowstyle='-|>', arrowsize=20, edge_color='gray')

    # Draw node labels
    nx.draw_networkx_labels(graph, pos, labels, font_size=12)

    # Draw edge labels
    edge_labels = nx.get_edge_attributes(graph, 'label')
    nx.draw_networkx_edge_labels(graph, pos, edge_labels=edge_labels, font_color='red', label_pos=0.5)

    # Set the plot title and turn off the axis
    plt.title('Knowledge Graph Visualization')
    plt.axis('off')
    
    # Save the plot to a file
    output_path = '/data/maochuanlu/Knowledge_graph/gpt_j_kg.png'
    plt.savefig(output_path, format='png', bbox_inches='tight')
    print(f"Graph saved to {output_path}")
    plt.show()  # Display the plot


def fetch_initial_relations(wikidata_item):
    relations = []
    if not wikidata_item:
        return relations
    for claim in wikidata_item.claims:
        target_items = wikidata_item.claims[claim]
        for target_item in target_items:
            target = target_item.getTarget()
            if isinstance(target, pywikibot.ItemPage):
                relations.append((claim, target.title()))
    return relations

def generate_relations(entity_label):
    prompt = (
        "Q: Javier Culson\n"
        "A: participant of # place of birth # sex or gender # country of citizenship # occupation # family name # given name # educated at # sport # sports discipline competed in\n"
        "Q: René Magritte\n"
        "A: ethnic group # place of birth # place of death # sex or gender # spouse # country of citizenship # member of political party # native language # place of burial # cause of death # residence # family name # given name # manner of death # educated at # field of work # work location # represented by\n"
        "Q: Nadym\n"
        "A: country # capital of # coordinate location # population # area # elevation above sea level\n"
        "Q: Stryn\n"
        "A: significant event # head of government # country # capital # separated from\n"
        "Q: 1585\n"
        "A: said to be the same as # follows\n"
        "Q: Bornheim\n"
        "A: head of government # country # member of # coordinate location # population # area # elevation above sea level\n"
        "Q: Aló Presidente\n"
        "A: genre # country of origin # cast member # original network\n"
        f"Q: {entity_label}\n"
        "A:"
    )
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Answer the query exactly in the format of the provided examples, listing attributes separated by #."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    relations_text = response.choices[0].message['content'].strip()
    relations_text = relations_text.replace('\n', ' ')
    relations = [sanitize_input(r) for r in re.split(r'#\s*', relations_text)]
    print(f"Generated relations for '{entity_label}': {relations}")
    return relations


def construct_knowledge_graph(entity_label, max_depth=2, branch_limit=2, model_name='gpt2_xl'):

    graph = nx.DiGraph()
    queue = Queue()
    queue.put((entity_label, 0))  # Enqueue the initial node and its depth
    print(f"Starting graph construction with root entity ID: {entity_label}")

    while not queue.empty():
        current_label, current_depth = queue.get()
        print(f"Processing entity: {current_label} at depth: {current_depth}")

        if current_depth > max_depth:
            print("Current depth exceeds max depth, skipping...")
            continue  # Skip processing if the current depth exceeds the maximum depth

        #1. get the subject
        if not graph.has_node(current_label):
            graph.add_node(current_label)
            print(f"Added node: {current_label} at depth {current_depth}")

        if current_depth == max_depth:
            print(f"Reached maximum depth at node: {current_label}, not expanding further.")
            continue  # Do not expand nodes at the maximum depth

        #2. paraphrase subject
        paraphrases = paraphrase_subject(current_label)
        print(f"Paraphrases found for '{current_label}': {paraphrases}")

        # Initialize dictionary to store relations for each paraphrase
        paraphrase_relations = {paraphrase: set() for paraphrase in paraphrases}
        paraphrase_relations[current_label] = set()

        # Collect all possible relations for each paraphrase
        for paraphrase in paraphrases:
            #3. get the corresponding paraphrased subjects' ids
            paraphrase_id = resolve_wikidata_id([paraphrase])
            print(f"Resolved Wikidata ID for paraphrase '{paraphrase}': {paraphrase_id}")

            if paraphrase_id:
                item = robust_request(paraphrase_id)
                #4. use wikidata to find relation for each paraphrased subject
                relations = fetch_initial_relations(item)
                print(f"Initial relations fetched for paraphrase '{paraphrase}': {relations}")

                #5. collect all relations for each paraphrased subject
                for rel_id, _ in relations:
                    paraphrase_relations[paraphrase].add(rel_id)

        #6. calculate intersection of all relation sets and filter with our_relations
        valid_our_relations = {v for v in our_relations.values() if v}
        print(f"valid relations: {valid_our_relations}")
        val_paraphrased_relations = paraphrase_relations.values()
        print(f"paraphrase_relations.values(): {val_paraphrased_relations}")

        common_relation_ids = set()
        for paraphrase_relation_sets in paraphrase_relations.values():
            common_relations = paraphrase_relation_sets.intersection(valid_our_relations)
            common_relation_ids = common_relation_ids.union(common_relations)

        #common_relation_ids = set.intersection(*paraphrase_relations.values(), valid_our_relations)
        print(f"Common relation IDs across all paraphrases and our_relations: {common_relation_ids}")


        #7. if we cannot find any intersections in these relations then we call generate_relations which call openai to generate relation
        #otherwise, just use fetch_label_by_id to transfer each relation_ids to relation_labels
        if not common_relation_ids:
            print("No common relations found, generating new relations...")
            common_relation_labels = generate_relations(current_label)  
            print(f"Generated relations: {common_relation_ids}")
        else: 
            common_relation_labels = {fetch_label_by_id(rel_id) for rel_id in common_relation_ids}
        

        #8.for all common_relations, we paraphrase them
        branches_created = 0
        for relation_label in common_relation_labels:
            if branches_created >= branch_limit:
                print("Branch limit reached, not creating more branches.")
                break
            # = paraphrase_relation(relation_label)
            #print(f"Paraphrases for relation '{relation_label}': {relation_paraphrases}")

            #9. for one paraphrased relation and original subject (not paraphrased one), we generate object 
            #NOTE: there are two versions of generating objects, one is only accept objects that were generated by at least two realizations of the relation to improve precision.
            #But if we do this, by openai sometimes will not generate any valid object
            #The other is to pick the first paraphrased_relation to generate object. But that precision will be lower. Here is the second version.
            #First version is commented
            # object_counter = {}
            #for relation_paraphrase in relation_paraphrases:
            generated_object = generate_object(current_label, relation_label, model_name)
            print(f"generated_object: {generated_object}")
            if not graph.has_edge(current_label, generated_object):
                graph.add_edge(current_label, generated_object, label=relation_label)
                print(f"Added edge from '{current_label}' to '{generated_object}' with relation '{relation_label}' at depth {current_depth}")
                queue.put((generated_object, current_depth + 1))
                branches_created += 1
            #     if generated_object in object_counter:
            #         object_counter[generated_object] += 1
            #     else:
            #         object_counter[generated_object] = 1
            # for generated_object, count in object_counter.items():
            #     if count >= 2 and not graph.has_edge(current_label, generated_object):
            #         graph.add_edge(current_label, generated_object, label=relation_paraphrase)
            #         print(f"Added edge from '{current_label}' to '{generated_object}' with relation '{relation_paraphrase}' at depth {current_depth}")
            #         if current_depth + 1 <= max_depth:
            #             queue.put((generated_object, current_depth + 1))

            #         branches_created += 1
            #         if branches_created >= branch_limit:
            #             print("Branch limit reached, not creating more branches.")
            #             break

    print("Graph construction completed.")
    return graph



def main():
    root_entity_id = "Q76" #obama
    root_entity_label = fetch_label_by_id(root_entity_id)
    # root_entity_label = "Maochuan Lu"
    max_depth = 2
    model_name = 'llama2'
    graph = construct_knowledge_graph(root_entity_label, max_depth, branch_limit = 3, model_name = model_name)
    visualize_graph(graph)

if __name__ == "__main__":
    main()
